"""
Пропуск в свечах закрывается, а если не закрылся — виден в журнале.

ОТКУДА ЭТО. 15 августа бот стартовал после 193-часового перерыва. Свечи брались
одним запросом без `since`, а биржа без него отдаёт ПОСЛЕДНИЕ limit свечей от
текущего момента — прошлое так не запросить в принципе. Всё, что раньше 41.7
часа (500 свечей по пять минут), терялось: код писал предупреждение и шёл дальше.

Из 23 сделок девять оказались испорчены. Шесть растянулись через пропуск:
позиция «продолжалась» с неверной цены, а пик хода мерился по обрезанному
куску — цифры вроде «дошло до 3.39R» были не рынком, а следом дыры. Ещё три
налились просроченными заявками (см. test_expired_order_never_fills).

В журнале испорченные сделки выглядели как все прочие. Разбор по ним давал
уверенные и неверные ответы — например, «прибыль надо отпускать бежать, там
2.29R»; на чистых данных недобор оказался 0.28R, то есть бежать было некуда.
Опознать испорченные удалось только по косвенному признаку.

Отсюда две проверки: промежуток берётся страницами, а остаток дыры сделка
называет сама.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BAR_MS = 300_000


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    """
    Брокер со своей папкой данных.

    ПРЕЖНИЕ МОДУЛИ ВОЗВРАЩАЮТСЯ НА МЕСТО, А НЕ УДАЛЯЮТСЯ. `config` читает
    BOT_DATA_DIR при импорте, поэтому его приходится выбрасывать из
    sys.modules и импортировать заново — иначе брокер пишет в настоящую папку
    данных.

    Оставить в кэше свой config нельзя: он смотрит во временную папку,
    которой после теста уже нет, и следующий по алфавиту файл получит его
    вместо настоящего. Так этот фикстур уронил четыре проверки в
    test_capital_split, проходившие по отдельности.

    Но и просто удалить его нельзя: test_capital_split держит ссылку на
    модуль и делает importlib.reload, а reload на удалённом из кэша модуле
    падает с ImportError. Поэтому запоминаем и кладём обратно.
    """
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    saved = {m: sys.modules.pop(m, None) for m in ('config', 'paper_broker')}
    import paper_broker
    yield paper_broker
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class FakeExchange:
    """
    Биржа, отдающая не больше `page` свечей за запрос — как настоящая.

    Без `since` возвращает последние: ровно то поведение, на котором терялась
    история.
    """

    def __init__(self, first_ts, count, page=500):
        self.bars = [[first_ts + i * BAR_MS, 1.0, 1.0, 1.0, 1.0, 0.0]
                     for i in range(count)]
        self.page = page
        self.calls = []

    def fetch_ohlcv(self, pair, tf, since=None, limit=500):
        self.calls.append({'since': since, 'limit': limit})
        rows = self.bars if since is None else [b for b in self.bars if b[0] >= since]
        take = min(limit, self.page)
        return (rows[-take:] if since is None else rows[:take])


def make(broker_mod, exchange):
    inst = broker_mod.PaperBroker.__new__(broker_mod.PaperBroker)
    inst.client = exchange
    inst.state = {
        'pending': {'FIBO': {}}, 'positions': {'FIBO': {}},
        'cooldown': {'FIBO': {}}, 'next_trade_id': 1,
        'balances': {'FIBO': 10000.0}, 'trades': [],
    }
    inst.strategies = ('FIBO',)
    inst._save_state = lambda: None
    return inst


class TestTheGapIsFetchedInPages:

    def test_a_long_break_is_covered_beyond_one_page(self, broker, monkeypatch):
        """
        193 часа — это 2316 свечей, впятеро больше одной выдачи. Раньше
        доезжали последние 500 и молчали об остальных.
        """
        now = 2_000_000_000_000
        monkeypatch.setattr(broker, '_now_ms', lambda: now)
        span = int(193 * 3_600_000)
        ex = FakeExchange(now - span, span // BAR_MS + 1)
        inst = make(broker, ex)

        got = inst._fetch_candles('BTCUSDT', now - span)

        assert len(got) > 500, (
            f'доехало {len(got)} свечей — промежуток по-прежнему обрезан '
            f'одной выдачей')
        assert len(ex.calls) > 1, 'запрос был один — страниц не было'
        assert all(c.get('since') for c in ex.calls), (
            'запрос без since: биржа отдаёт последние свечи, а не нужные')

    def test_the_candles_come_back_in_order_and_without_repeats(self, broker,
                                                               monkeypatch):
        """Склейка страниц не имеет права дублировать или переставлять свечи."""
        now = 2_000_000_000_000
        monkeypatch.setattr(broker, '_now_ms', lambda: now)
        span = int(100 * 3_600_000)
        ex = FakeExchange(now - span, span // BAR_MS + 1)
        inst = make(broker, ex)

        stamps = [c[0] for c in inst._fetch_candles('BTCUSDT', now - span)]

        assert stamps == sorted(stamps), 'свечи вернулись не по порядку'
        assert len(stamps) == len(set(stamps)), 'страницы наложились друг на друга'

    def test_the_forming_candle_is_left_out(self, broker, monkeypatch):
        """У текущей свечи high/low ещё не окончательны — она не в счёт."""
        now = 2_000_000_000_000
        monkeypatch.setattr(broker, '_now_ms', lambda: now)
        ex = FakeExchange(now - 20 * BAR_MS, 21)
        inst = make(broker, ex)
        got = inst._fetch_candles('BTCUSDT', now - 20 * BAR_MS)
        assert all(c[0] + BAR_MS <= now for c in got)

    def test_a_stalled_exchange_does_not_spin_forever(self, broker, monkeypatch):
        """Биржа, отдающая один и тот же кусок, не имеет права зациклить нас."""
        now = 2_000_000_000_000
        monkeypatch.setattr(broker, '_now_ms', lambda: now)

        class Stuck(FakeExchange):
            def fetch_ohlcv(self, pair, tf, since=None, limit=500):
                self.calls.append({'since': since, 'limit': limit})
                return self.bars[:3]

        ex = Stuck(now - int(200 * 3_600_000), 3000)
        inst = make(broker, ex)
        inst._fetch_candles('BTCUSDT', now - int(200 * 3_600_000))
        assert len(ex.calls) <= inst.MAX_PAGES

    def test_a_broken_request_does_not_crash_the_bot(self, broker, monkeypatch):
        now = 2_000_000_000_000
        monkeypatch.setattr(broker, '_now_ms', lambda: now)

        class Broken(FakeExchange):
            def fetch_ohlcv(self, *a, **k):
                raise RuntimeError('сеть отвалилась')

        inst = make(broker, Broken(now - 10 * BAR_MS, 10))
        assert inst._fetch_candles('BTCUSDT', now - 10 * BAR_MS) == []

    def test_the_normal_case_still_costs_one_request(self, broker, monkeypatch):
        """
        Пагинация не должна превращать обычный цикл в веер запросов: бот
        ходит по всем парам каждые несколько минут.
        """
        now = 2_000_000_000_000
        monkeypatch.setattr(broker, '_now_ms', lambda: now)
        ex = FakeExchange(now - 5 * BAR_MS, 6)
        inst = make(broker, ex)
        inst._fetch_candles('BTCUSDT', now - BAR_MS * 2)
        assert len(ex.calls) == 1, f'на свежую пару ушло {len(ex.calls)} запросов'


class TestATradeDeclaresItsOwnHole:

    def _position(self, last_ts):
        """Форма — как её создаёт _fill, иначе проверка мерит не то."""
        return {
            'trade_id': 1, 'strategy': 'FIBO', 'pair': 'BTCUSDT',
            'direction': 'LONG', 'entry_price': 100.0, 'planned_entry': 100.0,
            'size': 10.0, 'initial_size': 10.0,
            'stop_loss': 95.0, 'initial_stop': 95.0,
            'targets': [110.0], 'fractions': [1.0], 'be_level': None,
            'breakeven_after_tp': False, 'risk_amount': 50.0, 'rr': 2.0,
            'opened_ts': last_ts, 'opened_at': '', 'placed_ts': last_ts,
            'last_ts': last_ts, 'last_price': 100.0, 'tp_hit': 0,
            'realized_pnl': 0.0, 'fees_paid': 0.0, 'funding_paid': 0.0,
            'funding_ts': last_ts, 'breakeven_set': False,
            'mfe_price': 100.0, 'mae_price': 100.0,
            'balance_before': 10000.0, 'zone': '—', 'context': {},
        }

    def test_a_hole_is_counted(self, broker):
        """Свеча пришла на два часа позже предыдущей — это дыра, а не тик."""
        inst = make(broker, None)
        pos = self._position(1_000_000)
        inst.state['positions']['FIBO']['BTCUSDT'] = pos
        late = 1_000_000 + int(2 * 3_600_000)
        inst._advance('FIBO', 'BTCUSDT', [[late, 100, 100.5, 99.5, 100, 0]], 0.0)
        assert pos.get('gap_ms', 0) > 0, 'дыра не замечена'
        assert abs(pos['gap_ms'] - (2 * 3_600_000 - BAR_MS)) < BAR_MS

    def test_an_unbroken_stream_has_no_hole(self, broker):
        """Соседние свечи дырой не считаются — иначе метка обесценится."""
        inst = make(broker, None)
        pos = self._position(1_000_000)
        inst.state['positions']['FIBO']['BTCUSDT'] = pos
        bars = [[1_000_000 + i * BAR_MS, 100, 100.5, 99.5, 100, 0]
                for i in range(1, 5)]
        inst._advance('FIBO', 'BTCUSDT', bars, 0.0)
        assert pos.get('gap_ms', 0) == 0

    def test_holes_add_up(self, broker):
        inst = make(broker, None)
        pos = self._position(1_000_000)
        inst.state['positions']['FIBO']['BTCUSDT'] = pos
        t = 1_000_000
        for _ in range(3):
            t += int(1 * 3_600_000)
            inst._advance('FIBO', 'BTCUSDT', [[t, 100, 100.5, 99.5, 100, 0]], 0.0)
        assert pos['gap_ms'] > 2 * 3_600_000, 'дыры не суммируются'


class TestTheJournalCarriesTheMark:

    def test_the_column_exists(self, broker):
        assert 'data_gap_min' in broker.COLUMNS, (
            'колонки нет в выгрузке — испорченную сделку снова не отличить')

    def test_a_clean_trade_reports_zero(self, broker):
        inst = make(broker, None)
        pos = TestATradeDeclaresItsOwnHole()._position(1_000_000)
        row = inst._journal_row(pos, 1_000_000 + BAR_MS, 110.0, 'TP1',
                                100.0, 1.0, 0.0, 99.0, 10000.0, 10099.0)
        assert row['data_gap_min'] == 0

    def test_a_spoiled_trade_reports_its_minutes(self, broker):
        inst = make(broker, None)
        pos = TestATradeDeclaresItsOwnHole()._position(1_000_000)
        pos['gap_ms'] = int(193 * 3_600_000)
        row = inst._journal_row(pos, 1_000_000 + BAR_MS, 110.0, 'TP1',
                                100.0, 1.0, 0.0, 99.0, 10000.0, 10099.0)
        assert row['data_gap_min'] == 193 * 60, (
            'сделка, пережившая 193-часовой пропуск, не назвала его — '
            'именно так девять сделок молча испортили разбор')
