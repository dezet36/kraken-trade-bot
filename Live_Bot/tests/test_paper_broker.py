"""
Тесты фантомного брокера.

Что здесь проверяется по существу: фантомный счёт — единственный источник
выводов месячного эксперимента. Ошибка в модели исполнения не уронит бота и
не появится в логах, она просто нарисует стратегии доходность, которой нет.
Поэтому тесты сосредоточены на способах соврать в свою пользу: заполнение
лимита там, где цена не была; тейк вместо стопа внутри одной свечи;
потерянные комиссии; общий слот на две стратегии.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BAR_MS = 5 * 60 * 1000


class FakeClient:
    """Свечи задаются тестом; ордера отправлять нечем — этого метода нет."""

    def __init__(self):
        self.candles = {}

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        return list(self.candles.get(symbol, []))

    def fetch_funding_rate(self, symbol):
        raise RuntimeError('ставка недоступна')


@pytest.fixture()
def broker_env(tmp_path, monkeypatch):
    """Изолированный фантомный счёт: своя папка данных, никаких издержек."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TRADING_MODE', 'PAPER')
    monkeypatch.setenv('PAPER_START_BALANCE', '10000')
    monkeypatch.setenv('PAPER_FUNDING', 'false')
    for module in ('config', 'paper_broker', 'dashboard'):
        sys.modules.pop(module, None)

    import config
    import paper_broker

    # Издержки по умолчанию выключаем: их считает отдельный тест, а в
    # остальных они только зашумляют ожидаемые числа.
    monkeypatch.setattr(config, 'PAPER_FEE_MAKER', 0.0)
    monkeypatch.setattr(config, 'PAPER_FEE_TAKER', 0.0)
    monkeypatch.setattr(config, 'PAPER_SLIPPAGE_PCT', 0.0)
    monkeypatch.setattr(config, 'RISK_PER_TRADE', 1.0)
    monkeypatch.setattr(config, 'LIMIT_ENTRY_OFFSET_PCT', 0.0)
    monkeypatch.setattr(config, 'MAX_POSITION_HOLD_HOURS', 0.0)
    monkeypatch.setattr(config, 'COOLDOWN_HOURS', 12)
    monkeypatch.setattr(config, 'TP_CLOSE_FRACTIONS', [1.0])

    client = FakeClient()
    broker = paper_broker.PaperBroker(client, strategies=('FIBO', 'SMC'))
    return broker, client, paper_broker, config


def signal(pair='BTCUSDT', direction='LONG', entry=100.0, stop=90.0,
           tp1=130.0, tp2=None, strategy='FIBO', targets=None, fractions=None,
           be_level=None, breakeven=True, cap=0):
    params = {
        'entry': entry, 'stop_loss': stop,
        'take_profit_1': tp1, 'take_profit_2': tp2 or tp1,
        'be_level': be_level, 'breakeven_after_tp': breakeven,
        'max_same_direction': cap,
        'tp_targets': list(targets) if targets else [tp1],
        'tp_fractions': list(fractions) if fractions else [1.0],
        'rr': abs(tp1 - entry) / abs(entry - stop),
        'position_size': 1.0, 'risk_amount': 100.0,
    }
    return {
        'trading_pair': pair,
        'strategy': strategy,
        'setup': {'type': direction, 'start_price': 80.0, 'end_price': 120.0, 'size': 40.0},
        'trigger': {'zone': 'Zone_A'},
        'htf_trend': 'BULLISH',
        'params': params,
        'scan': {'score': 71.5, 'proximity': 0.8},
    }


def candles(start_ts, bars):
    """bars: список (high, low, close) — открытие для модели не важно."""
    return [[start_ts + i * BAR_MS, low, high, low, close, 0]
            for i, (high, low, close) in enumerate(bars)]


def feed(broker, client, pair, bars, start_ts=None, now=None):
    """Подаёт свечи так, чтобы все они считались закрытыми."""
    import paper_broker as pb
    start = start_ts if start_ts is not None else 1_700_000_000_000
    client.candles[pair] = candles(start, bars)
    end = start + len(bars) * BAR_MS + BAR_MS
    pb._now_ms = lambda: (now or end)
    broker.update()


class TestFill:
    def test_limit_fills_only_when_price_touches_it(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        assert broker.open('FIBO', signal(entry=100.0))

        # Цена не дошла до лимита — позиции быть не должно
        feed(broker, client, 'BTCUSDT', [(105, 101, 103)])
        assert not broker.positions('FIBO')
        assert broker.pending('FIBO')

        # Свеча коснулась 100 — вход состоялся ровно по цене лимита
        feed(broker, client, 'BTCUSDT', [(104, 99.5, 102)],
             start_ts=1_700_000_000_000 + BAR_MS)
        position = broker.positions('FIBO')['BTCUSDT']
        assert position['entry_price'] == 100.0

    def test_fill_beats_invalidation_when_both_in_one_candle(self, broker_env):
        """
        Обвал прошёл и через лимит, и через уровень инвалидации.

        Лимит лежит ближе к рынку, значит цена достала его первой: сделка
        обязана открыться и тут же выйти по стопу. Снять ордер «задним числом»
        было бы подарком стратегии — убыток просто исчез бы из статистики.
        """
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0))

        feed(broker, client, 'BTCUSDT', [(101, 84.0, 85.0)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        rows = pb.read_journal()
        assert len(rows) == 1
        assert rows[0]['exit_reason'] == 'SL'

    def test_pending_dropped_when_price_left_without_us(self, broker_env):
        """Цена дошла до цели, так и не коснувшись лимита — сетап отработал без нас."""
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=130.0))

        feed(broker, client, 'BTCUSDT', [(131, 101, 130)])
        assert not broker.pending('FIBO')
        assert not broker.positions('FIBO')
        assert not pb.read_journal()   # несостоявшийся вход — не сделка

    def test_pending_expires(self, broker_env):
        broker, client, pb, cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0))
        broker.pending('FIBO')['BTCUSDT']['expires_ts'] = 1_700_000_000_000 + BAR_MS

        feed(broker, client, 'BTCUSDT', [(105, 101, 104), (105, 101, 104)])
        assert not broker.pending('FIBO')
        assert not broker.positions('FIBO')


class TestExit:
    def test_stop_wins_over_take_inside_one_candle(self, broker_env):
        """
        Свеча задела и стоп, и тейк. Порядок событий по OHLC неизвестен, и
        трактовать его в свою пользу — самый простой способ нарисовать
        доходность, которой не было. Засчитываем стоп.
        """
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=130.0))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (135, 85, 120)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        rows = pb.read_journal()
        assert len(rows) == 1
        assert rows[0]['exit_reason'] == 'SL'
        assert float(rows[0]['pnl_usd']) < 0

    def test_take_profit_closes_in_plus(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=130.0))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (131, 99, 130)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        rows = pb.read_journal()
        assert rows[0]['exit_reason'] == 'TP1'
        assert float(rows[0]['pnl_r']) == pytest.approx(3.0, abs=0.01)

    def test_short_stops_on_high(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(direction='SHORT', entry=100.0, stop=110.0, tp1=70.0))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (112, 99, 111)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        rows = pb.read_journal()
        assert rows[0]['exit_reason'] == 'SL'
        assert float(rows[0]['exit_price']) == pytest.approx(110.0)

    def test_time_stop_closes_stale_position(self, broker_env):
        broker, client, pb, cfg = broker_env
        import config
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=130.0))
        feed(broker, client, 'BTCUSDT', [(100, 100, 100)])
        assert broker.positions('FIBO')

        config.MAX_POSITION_HOLD_HOURS = 1.0
        try:
            feed(broker, client, 'BTCUSDT', [(101, 99, 100)],
                 start_ts=1_700_000_000_000 + 2 * 3_600_000,
                 now=1_700_000_000_000 + 3 * 3_600_000)
        finally:
            config.MAX_POSITION_HOLD_HOURS = 0.0

        rows = pb.read_journal()
        assert rows[0]['exit_reason'] == 'TIME'


class TestCosts:
    def test_fees_reduce_result(self, broker_env):
        """Комиссии обязаны уменьшать итог: без них месяц выглядит богаче, чем был."""
        broker, client, pb, _cfg = broker_env
        import config
        config.PAPER_FEE_MAKER = 0.0002
        config.PAPER_FEE_TAKER = 0.00055
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=130.0))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (131, 99, 130)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        row = pb.read_journal()[0]
        assert float(row['fees_usd']) > 0
        assert float(row['pnl_usd']) < float(row['gross_pnl_usd'])

    def test_slippage_worsens_stop_exit(self, broker_env):
        broker, client, pb, _cfg = broker_env
        import config
        config.PAPER_SLIPPAGE_PCT = 0.001
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=130.0))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (101, 85, 88)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        row = pb.read_journal()[0]
        assert float(row['exit_price']) < 90.0   # вышли хуже уровня стопа


class TestParallelStrategies:
    def test_both_strategies_hold_same_pair(self, broker_env):
        """
        Ради этого фантомный режим и делался: на бирже позиция по инструменту
        одна, и более частая стратегия отбирала бы сетапы у второй.
        """
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000

        assert broker.open('FIBO', signal(strategy='FIBO'))
        assert broker.open('SMC', signal(strategy='SMC', entry=100.0, tp1=150.0))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100)])
        assert 'BTCUSDT' in broker.positions('FIBO')
        assert 'BTCUSDT' in broker.positions('SMC')

    def test_gate_is_per_strategy(self, broker_env):
        broker, _client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal())

        assert broker.gate('FIBO').has_position_or_order('BTCUSDT')
        assert not broker.gate('SMC').has_position_or_order('BTCUSDT')

    def test_deposits_are_independent(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(strategy='SMC', entry=100.0, stop=90.0, tp1=130.0))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (131, 99, 130)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        assert broker.balance('SMC') > 10_000
        assert broker.balance('FIBO') == 10_000


class TestExitPlan:
    def test_partial_targets_are_executed(self, broker_env):
        """
        План SMC 25/25/50 должен исполняться целиком. Пока доли брались из
        глобального config, позиция закрывалась вся на первой цели — по
        бэктесту это убыточная конфигурация (−9.1% против +40.6%).
        """
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(strategy='SMC', entry=100.0, stop=90.0,
                                  targets=[110.0, 120.0, 140.0],
                                  fractions=[0.25, 0.25, 0.50], breakeven=False))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (121, 99, 120)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        position = broker.positions('SMC')['BTCUSDT']
        assert position['tp_hit'] == 2                       # две цели взяты
        assert position['size'] == pytest.approx(0.5 * 10.0)  # осталась половина
        assert not pb.read_journal()                          # сделка ещё открыта

    def test_last_target_closes_the_rest(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(strategy='SMC', entry=100.0, stop=90.0,
                                  targets=[110.0, 120.0, 140.0],
                                  fractions=[0.25, 0.25, 0.50], breakeven=False))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (141, 99, 140)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        row = pb.read_journal()[0]
        assert row['exit_reason'] == 'TP3'
        assert int(row['tps_hit']) == 2
        # Взвешенный результат: 0.25*1R + 0.25*2R + 0.5*4R = 2.75R
        assert float(row['pnl_r']) == pytest.approx(2.75, abs=0.02)

    def test_breakeven_off_lets_position_run(self, broker_env):
        """
        У SMC безубыток выключен намеренно. Если бы стоп подтягивался после
        первой цели, откат к входу закрыл бы позицию до дальних целей — а
        именно они и дают прибыль при винрейте около 25%.
        """
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(strategy='SMC', entry=100.0, stop=90.0,
                                  targets=[110.0, 140.0], fractions=[0.5, 0.5],
                                  breakeven=False))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (111, 99, 110),
                                         (105, 99.5, 100)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        position = broker.positions('SMC')['BTCUSDT']
        assert position['tp_hit'] == 1
        assert position['stop_loss'] == 90.0        # стоп не двинулся
        assert not position['breakeven_set']

    def test_breakeven_on_protects_position(self, broker_env):
        """У фибо безубыток включён — там он и был откалиброван."""
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=140.0,
                                   be_level=110.0, breakeven=True))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (111, 105, 110)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        position = broker.positions('FIBO')['BTCUSDT']
        assert position['breakeven_set']
        assert position['stop_loss'] == 100.0

    def test_breakeven_and_stop_in_one_candle_closes_position(self, broker_env):
        """
        Свеча и пробила уровень безубытка, и откатилась ниже входа. Порядок
        внутри свечи неизвестен, поэтому считаем, что стоп уже был подтянут и
        сработал — как и везде, спорная свеча трактуется против нас.
        """
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=140.0,
                                   be_level=110.0, breakeven=True))

        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (111, 99, 105)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        assert pb.read_journal()[0]['exit_reason'] == 'BE'


class TestDirectionCap:
    def test_cap_blocks_third_long_of_same_strategy(self, broker_env):
        broker, _client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000

        assert broker.open('SMC', signal(pair='BTCUSDT', strategy='SMC', cap=2))
        assert broker.open('SMC', signal(pair='ETHUSDT', strategy='SMC', cap=2))
        assert not broker.open('SMC', signal(pair='SOLUSDT', strategy='SMC', cap=2))

    def test_opposite_direction_still_allowed(self, broker_env):
        broker, _client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(pair='BTCUSDT', strategy='SMC', cap=1))

        assert broker.open('SMC', signal(pair='ETHUSDT', strategy='SMC', cap=1,
                                         direction='SHORT', entry=100.0,
                                         stop=110.0, tp1=70.0))

    def test_cap_of_one_strategy_does_not_limit_the_other(self, broker_env):
        """
        Книги стратегий раздельные. Кэп SMC не должен молча урезать фибо —
        иначе месячное сравнение мерило бы не стратегии, а их взаимные помехи.
        """
        broker, _client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(pair='BTCUSDT', strategy='SMC', cap=1))

        assert broker.open('FIBO', signal(pair='ETHUSDT', cap=0))
        assert broker.open('FIBO', signal(pair='SOLUSDT', cap=0))


class TestSizing:
    def test_risk_follows_own_deposit(self, broker_env):
        """Размер считается от депозита СВОЕЙ стратегии, а не от общего счёта."""
        broker, _client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.state['balance']['SMC'] = 20_000
        broker.open('SMC', signal(strategy='SMC', entry=100.0, stop=90.0))

        order = broker.pending('SMC')['BTCUSDT']
        assert order['risk_amount'] == pytest.approx(200.0)   # 1% от 20 000
        assert order['size'] == pytest.approx(20.0)           # 200 / 10

    def test_oversized_position_rejected(self, broker_env):
        """Позиция дороже депозита с плечом невозможна и на бирже."""
        broker, _client, pb, _cfg = broker_env
        import config
        config.LEVERAGE = 1
        try:
            pb._now_ms = lambda: 1_700_000_000_000
            # стоп в 0.1% от цены -> объём в 1000 раз больше риска
            assert not broker.open('FIBO', signal(entry=100.0, stop=99.9))
        finally:
            config.LEVERAGE = 20


class TestPersistence:
    def test_state_survives_restart(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal())
        feed(broker, client, 'BTCUSDT', [(100, 100, 100)])

        revived = pb.PaperBroker(client, strategies=('FIBO', 'SMC'))
        assert 'BTCUSDT' in revived.positions('FIBO')
        assert revived.balance('FIBO') == 10_000

    def test_changed_deposit_does_not_rewrite_history(self, broker_env):
        """
        Депозит поменяли в .env после старта. Пересчитать базу задним числом
        значило бы исказить всю доходность эксперимента.
        """
        broker, client, pb, _cfg = broker_env
        revived = pb.PaperBroker(client, strategies=('FIBO', 'SMC'),
                                 start_balance={'FIBO': 50_000, 'SMC': 50_000})
        assert revived.start_balance('FIBO') == 10_000


class TestDeposit:
    """
    Депозит — база расчёта доходности. Сменить его и оставить прежние сделки
    значит получить процент, которого никогда не было.
    """

    def test_set_freely_before_first_trade(self, broker_env):
        broker, _client, _pb, _cfg = broker_env
        ok, _msg = broker.set_deposit('SMC', 25_000)

        assert ok
        assert broker.start_balance('SMC') == 25_000
        assert broker.balance('SMC') == 25_000

    def test_refused_after_trading_started(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(strategy='SMC'))

        ok, message = broker.set_deposit('SMC', 25_000)
        assert not ok
        assert 'перезапуск' in message.lower()
        assert broker.start_balance('SMC') == 10_000

    def test_restart_sets_new_base_and_clears_book(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(strategy='SMC'))
        feed(broker, client, 'BTCUSDT', [(100, 100, 100)])
        assert broker.positions('SMC')

        ok, _msg = broker.set_deposit('SMC', 25_000, restart=True)

        assert ok
        assert broker.start_balance('SMC') == 25_000
        assert not broker.positions('SMC') and not broker.pending('SMC')
        assert broker.reset_at('SMC') is not None

    def test_restart_does_not_touch_the_other_strategy(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(pair='ETHUSDT'))
        broker.set_deposit('SMC', 25_000, restart=True)

        assert broker.pending('FIBO')
        assert broker.start_balance('FIBO') == 10_000

    def test_history_is_not_deleted(self, broker_env):
        """
        Перезапуск исключает прежние сделки из статистики, но не стирает их:
        это данные для разбора стратегии, и терять их из-за смены депозита
        было бы обидно.
        """
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('SMC', signal(strategy='SMC', entry=100.0, stop=90.0, tp1=130.0))
        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (131, 99, 130)],
             now=1_700_000_000_000 + 5 * BAR_MS)
        assert len(pb.read_journal()) == 1

        broker.set_deposit('SMC', 25_000, restart=True)
        assert len(pb.read_journal()) == 1

    def test_nonsense_deposit_rejected(self, broker_env):
        broker, _client, _pb, _cfg = broker_env

        assert broker.set_deposit('SMC', -100)[0] is False
        assert broker.set_deposit('SMC', 'много')[0] is False
        assert broker.set_deposit('НЕТ ТАКОЙ', 1000)[0] is False


class TestSnapshot:
    def test_snapshot_shows_unrealised_and_reason(self, broker_env):
        broker, client, pb, _cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker.open('FIBO', signal(entry=100.0, stop=90.0, tp1=130.0))
        feed(broker, client, 'BTCUSDT', [(100, 100, 100), (110, 99, 110)],
             now=1_700_000_000_000 + 5 * BAR_MS)

        snap = broker.snapshot()
        position = [p for p in snap['open'] if not p.get('pending')][0]
        assert position['unrealised'] > 0
        # Обоснование входа читается человеком, без словаря технических имён
        assert 'Лонг' in position['why'] and 'зона A' in position['why']
        assert snap['strategies']['FIBO']['start_balance'] == 10_000
