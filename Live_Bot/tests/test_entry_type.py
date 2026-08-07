"""
Тип входа, объявленный стратегией, должен исполняться, а не игнорироваться.

ОТКУДА ЭТО. Оператор заметил, что лимитные заявки по уровням и Боллинджеру
появляются и пропадают, ничего не открыв. Разбор показал две вещи.

1. Поле `entry_type` объявляли ВСЕ четыре стратегии — и не читал НИКТО.
   Бумажный брокер исполнял каждый вход как лимит на откате.

   Для уровней это ровно наоборот тому, что мерилось. Они входят ПО ХОДУ
   движения: замер ставил им stop-заявку, срабатывающую при пробое цены
   возврата в сторону сделки. Лимит наливается только если цена вернётся
   НАЗАД. Условия противоположные, и последствие злое: когда сделка шла как
   надо, заявка не наливалась, а после снималась с пометкой «цена дошла до
   цели без нас». Стратегия систематически пропускала свои удачные сценарии —
   и это же объясняет, почему по ней месяцами почти не было сделок.

2. Срок жизни неналитой заявки брался из config.PENDING_ORDER_MAX_HOURS —
   параметра Фибоначчи, 72 часа. Уровни мерились на 24 часах, Боллинджер на
   шести: их заявки жили втрое и вдвенадцатеро дольше, чем в замере, занимая
   слот и держа кулдаун по паре.

Оба дефекта — из одного семейства «бот торгует не то, что измерено», и оба
проверяются здесь.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('config', 'paper_broker'):
        sys.modules.pop(module, None)
    import paper_broker
    return paper_broker


def an_order(direction='LONG', entry_type='LIMIT', limit=100.0):
    return {
        'strategy': 'LEVELS', 'pair': 'BTCUSDT', 'direction': direction,
        'planned_entry': limit, 'limit_price': limit, 'stop_loss': limit - 5,
        'targets': [limit + 10], 'fractions': [1.0], 'be_level': None,
        'breakeven_after_tp': False, 'risk_amount': 50.0, 'size': 10.0,
        'rr': 2.0, 'invalidation': None, 'placed_ts': 0, 'placed_at': '',
        'entry_type': entry_type, 'expires_ts': 10 ** 15, 'last_ts': 0,
        'balance_before': 10000.0, 'context': {},
    }


def make(broker_mod, tmp_path):
    inst = broker_mod.PaperBroker.__new__(broker_mod.PaperBroker)
    inst.state = {
        'pending': {'LEVELS': {}}, 'positions': {'LEVELS': {}},
        'cooldown': {'LEVELS': {}}, 'next_trade_id': 1,
        'balances': {'LEVELS': 10000.0}, 'trades': [],
    }
    inst.strategies = ('LEVELS',)
    inst._save_state = lambda: None
    return inst


class TestStopEntryFillsOnTheRightSide:
    """
    Вход по ходу движения наливается, когда цена ПРОБИВАЕТ уровень вверх (для
    лонга). Лимит на откате — когда цена опускается к нему. Это разные события,
    и путать их значит торговать другую стратегию.
    """

    def test_stop_entry_fills_when_price_breaks_up(self, broker, tmp_path):
        inst = make(broker, tmp_path)
        order = an_order(entry_type='MARKET')
        inst.state['pending']['LEVELS']['BTCUSDT'] = order
        # Свеча ушла ВВЕРХ через уровень и вниз к нему не возвращалась.
        inst._process_pending('LEVELS', 'BTCUSDT', order, 1_000,
                              high=105.0, low=101.0, open_price=101.0)
        assert 'BTCUSDT' in inst.positions('LEVELS'), (
            'вход по ходу движения не налился на пробое — это тот самый '
            'дефект, из-за которого уровни пропускали свои сценарии')

    def test_stop_entry_ignores_a_pullback(self, broker, tmp_path):
        inst = make(broker, tmp_path)
        order = an_order(entry_type='MARKET')
        inst.state['pending']['LEVELS']['BTCUSDT'] = order
        # Цена только опускалась: для stop-заявки это не событие.
        inst._process_pending('LEVELS', 'BTCUSDT', order, 1_000,
                              high=99.0, low=95.0, open_price=99.0)
        assert 'BTCUSDT' not in inst.positions('LEVELS')

    def test_limit_entry_still_fills_on_a_pullback(self, broker, tmp_path):
        """Контроль: у лимитных стратегий ничего не изменилось."""
        inst = make(broker, tmp_path)
        order = an_order(entry_type='LIMIT')
        inst.state['pending']['LEVELS']['BTCUSDT'] = order
        inst._process_pending('LEVELS', 'BTCUSDT', order, 1_000,
                              high=101.0, low=99.0, open_price=101.0)
        assert 'BTCUSDT' in inst.positions('LEVELS')

    def test_limit_entry_ignores_a_breakout(self, broker, tmp_path):
        inst = make(broker, tmp_path)
        order = an_order(entry_type='LIMIT')
        inst.state['pending']['LEVELS']['BTCUSDT'] = order
        inst._process_pending('LEVELS', 'BTCUSDT', order, 1_000,
                              high=110.0, low=100.5, open_price=100.5)
        assert 'BTCUSDT' not in inst.positions('LEVELS')


class TestGapIsNotAGift:
    def test_gap_through_the_level_fills_at_the_open(self, broker, tmp_path):
        """
        Разрыв через уровень исполняется по открытию, а не по цене заявки.
        Обратное дарило бы стратегии лучшую цену ровно тогда, когда рынок ушёл
        против неё. Тем же правилом живёт движок замеров.
        """
        inst = make(broker, tmp_path)
        order = an_order(entry_type='MARKET')
        inst.state['pending']['LEVELS']['BTCUSDT'] = order
        inst._process_pending('LEVELS', 'BTCUSDT', order, 1_000,
                              high=112.0, low=103.0, open_price=103.0)
        position = inst.positions('LEVELS')['BTCUSDT']
        assert position['entry_price'] == pytest.approx(103.0)


class TestFeeFollowsTheEntryType:
    def test_stop_entry_pays_taker(self, broker, tmp_path):
        import config
        inst = make(broker, tmp_path)
        order = an_order(entry_type='MARKET')
        inst.state['pending']['LEVELS']['BTCUSDT'] = order
        inst._process_pending('LEVELS', 'BTCUSDT', order, 1_000,
                              high=105.0, low=101.0, open_price=101.0)
        position = inst.positions('LEVELS')['BTCUSDT']
        expected = order['size'] * position['entry_price'] * config.PAPER_FEE_TAKER
        assert position['fees_paid'] == pytest.approx(expected)

    def test_limit_entry_pays_maker(self, broker, tmp_path):
        import config
        inst = make(broker, tmp_path)
        order = an_order(entry_type='LIMIT')
        inst.state['pending']['LEVELS']['BTCUSDT'] = order
        inst._process_pending('LEVELS', 'BTCUSDT', order, 1_000,
                              high=101.0, low=99.0, open_price=101.0)
        position = inst.positions('LEVELS')['BTCUSDT']
        expected = order['size'] * 100.0 * config.PAPER_FEE_MAKER
        assert position['fees_paid'] == pytest.approx(expected)


class TestExpiryComesFromTheStrategy:
    def test_each_strategy_gets_its_own(self, broker):
        import config
        from levels import params as levels_params
        from rsibb import params as rsibb_params

        assert broker.PaperBroker._expiry_hours('LEVELS') == pytest.approx(
            levels_params.EXPIRY_HOURS)
        assert broker.PaperBroker._expiry_hours('RSIBB') == pytest.approx(
            rsibb_params.EXPIRY_BARS * broker._bar_hours(rsibb_params.TIMEFRAME))
        # У Фибоначчи своего параметра нет — остаётся общий.
        assert broker.PaperBroker._expiry_hours('FIBO') == pytest.approx(
            config.PENDING_ORDER_MAX_HOURS)

    def test_levels_do_not_inherit_the_fibo_window(self, broker):
        import config
        assert broker.PaperBroker._expiry_hours('LEVELS') != pytest.approx(
            config.PENDING_ORDER_MAX_HOURS), (
            'уровни снова живут по сроку Фибоначчи')


class TestSignalsDeclareTheirType:
    def test_every_strategy_declares_entry_type(self):
        """
        Поле обязательно у всех: молчаливое умолчание вернуло бы уровням
        лимитное исполнение, и дефект возродился бы без единой ошибки.
        """
        import re
        bot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ('strategy.py', 'strategy_smc.py', 'strategy_levels.py',
                     'strategy_rsibb.py'):
            text = open(os.path.join(bot, name), encoding='utf-8').read()
            assert re.search(r"'entry_type':", text), f'{name}: нет entry_type'
