"""
Просроченная заявка не исполняется. Никогда.

ОТКУДА ЭТО. В журнале нашлись три сделки SMC — #15, #16 и #17, — открытые и
закрытые в ОДНУ И ТУ ЖЕ секунду 2026-08-13T03:25:00. Держались 0 минут, хода в
нашу сторону не было вовсе, а ход против нас внутри «одной свечи» составил
−1.27R, −3.19R и −1.52R. Свечей такого размаха не бывает: одна свеча перекрыла
весь интервал, пока бот не работал.

Заявки к тому моменту простояли 152, 183 и 152 часа при сроке жизни 72. То
есть они были мертвы трое-четверо суток и всё равно налились — и тут же вынесли
стоп. Минус $172.07, или 59% всего убытка стратегии.

ПРИЧИНА. В `_process_pending` проверка срока стояла ПОСЛЕ проверки цены. До неё
просто не доходило: заявка наливалась и возвращала управление. Пока бот
работает, это незаметно — заявку смотрят каждую свечу, и она умирает на первой
же свече после срока. Но пока бот стоит, свечей никто не смотрит, а на запуске
поток продолжается с текущей свечи.

ПРАВИЛО. Свеча, открывшаяся после срока, заявку уже не застаёт. Если срок падает
ВНУТРЬ свечи — на её открытии заявка была жива, и такое заполнение честное.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOUR = 3_600_000


@pytest.fixture()
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('config', 'paper_broker'):
        sys.modules.pop(module, None)
    import paper_broker
    return paper_broker


def an_order(expires_ts, direction='LONG', limit=100.0, entry_type='LIMIT'):
    return {
        'strategy': 'SMC', 'pair': 'BTCUSDT', 'direction': direction,
        'planned_entry': limit, 'limit_price': limit, 'stop_loss': limit - 5,
        'targets': [limit + 10], 'fractions': [1.0], 'be_level': None,
        'breakeven_after_tp': False, 'risk_amount': 50.0, 'size': 10.0,
        'rr': 2.0, 'invalidation': None, 'placed_ts': 0, 'placed_at': '',
        'entry_type': entry_type, 'expires_ts': expires_ts, 'last_ts': 0,
        'balance_before': 10000.0, 'context': {},
    }


def make(broker_mod):
    inst = broker_mod.PaperBroker.__new__(broker_mod.PaperBroker)
    inst.state = {
        'pending': {'SMC': {}}, 'positions': {'SMC': {}},
        'cooldown': {'SMC': {}}, 'next_trade_id': 1,
        'balances': {'SMC': 10000.0}, 'trades': [],
    }
    inst.strategies = ('SMC',)
    inst._save_state = lambda: None
    return inst


class TestTheDeadlineOutranksThePrice:

    def test_an_expired_order_does_not_fill(self, broker):
        """
        Ровно тот случай. Заявка просрочена на трое суток, приходит свеча,
        накрывающая её цену, — и заявка обязана умереть, а не открыть позицию.
        """
        inst = make(broker)
        order = an_order(expires_ts=72 * HOUR)
        inst.state['pending']['SMC']['BTCUSDT'] = order
        # Свеча пришла на 152-м часу — как у #15 и #17 — и цену накрывает.
        inst._process_pending('SMC', 'BTCUSDT', order, 152 * HOUR,
                              high=101.0, low=95.0, open_price=101.0)
        assert 'BTCUSDT' not in inst.positions('SMC'), (
            'просроченная заявка открыла позицию — это дефект, стоивший '
            '$172.07 на трёх сделках')

    def test_it_is_removed_rather_than_left_hanging(self, broker):
        """Снятие обязано быть настоящим: иначе она попробует ещё раз."""
        inst = make(broker)
        order = an_order(expires_ts=72 * HOUR)
        inst.state['pending']['SMC']['BTCUSDT'] = order
        inst._process_pending('SMC', 'BTCUSDT', order, 152 * HOUR,
                              high=101.0, low=95.0, open_price=101.0)
        assert 'BTCUSDT' not in inst.pending('SMC')

    def test_a_long_gap_does_not_resurrect_it(self, broker):
        """
        Простой в неделю — обычное дело для настольного приложения: человек
        выключил компьютер. Возвращение не должно открывать недельные сделки.
        """
        inst = make(broker)
        order = an_order(expires_ts=72 * HOUR)
        inst.state['pending']['SMC']['BTCUSDT'] = order
        inst._process_pending('SMC', 'BTCUSDT', order, 183 * HOUR,
                              high=140.0, low=60.0, open_price=90.0)
        assert 'BTCUSDT' not in inst.positions('SMC')

    def test_a_short_order_is_held_to_the_same_rule(self, broker):
        """Сторона сделки к сроку отношения не имеет."""
        inst = make(broker)
        order = an_order(expires_ts=72 * HOUR, direction='SHORT')
        inst.state['pending']['SMC']['BTCUSDT'] = order
        inst._process_pending('SMC', 'BTCUSDT', order, 152 * HOUR,
                              high=105.0, low=99.0, open_price=99.0)
        assert 'BTCUSDT' not in inst.positions('SMC')

    def test_a_stop_entry_is_held_to_the_same_rule(self, broker):
        """И тип входа тоже."""
        inst = make(broker)
        order = an_order(expires_ts=72 * HOUR, entry_type='MARKET')
        inst.state['pending']['SMC']['BTCUSDT'] = order
        inst._process_pending('SMC', 'BTCUSDT', order, 152 * HOUR,
                              high=110.0, low=100.5, open_price=100.5)
        assert 'BTCUSDT' not in inst.positions('SMC')


class TestLiveOrdersAreUntouched:
    """
    Правило обязано резать только мёртвое. Если оно заодно режет живые заявки,
    лечение хуже болезни: стратегия перестанет торговать вовсе.
    """

    def test_an_order_within_its_life_still_fills(self, broker):
        inst = make(broker)
        order = an_order(expires_ts=72 * HOUR)
        inst.state['pending']['SMC']['BTCUSDT'] = order
        inst._process_pending('SMC', 'BTCUSDT', order, 40 * HOUR,
                              high=101.0, low=99.0, open_price=101.0)
        assert 'BTCUSDT' in inst.positions('SMC'), (
            'живая заявка перестала наливаться — правило зарезало лишнее')

    def test_the_deadline_inside_the_candle_still_fills(self, broker):
        """
        Свеча открылась ДО срока: на её открытии заявка была жива. Внутри
        свечи порядок событий неизвестен, и отнимать такое заполнение не за
        что — граница проходит по открытию, а не по закрытию.
        """
        inst = make(broker)
        order = an_order(expires_ts=72 * HOUR)
        inst.state['pending']['SMC']['BTCUSDT'] = order
        inst._process_pending('SMC', 'BTCUSDT', order, 72 * HOUR - 1,
                              high=101.0, low=99.0, open_price=101.0)
        assert 'BTCUSDT' in inst.positions('SMC')

    def test_the_very_first_candle_after_the_deadline_is_already_late(self, broker):
        """Граница ровно на сроке: свеча, открывшаяся В срок, — уже поздно."""
        inst = make(broker)
        order = an_order(expires_ts=72 * HOUR)
        inst.state['pending']['SMC']['BTCUSDT'] = order
        inst._process_pending('SMC', 'BTCUSDT', order, 72 * HOUR,
                              high=101.0, low=99.0, open_price=101.0)
        assert 'BTCUSDT' not in inst.positions('SMC')


class TestTheReasonNamesTheRightNumber:

    def test_each_strategy_reports_its_own_limit(self, broker):
        """
        В снятии стояло config.PENDING_ORDER_MAX_HOURS — 72 часа, параметр
        Фибоначчи. Уровни живут 24 часа, Боллинджер — шесть. Заявку снимали
        верно, а причину писали чужую, и в журнале это выглядело ошибкой.
        """
        assert broker.PaperBroker._expiry_hours('LEVELS') == 24.0
        assert broker.PaperBroker._expiry_hours('SMC') == 72.0

    def test_the_message_carries_that_number(self, broker):
        inst = make(broker)
        said = []
        inst._drop_pending = lambda s, p, why: said.append(why)
        order = an_order(expires_ts=72 * HOUR)
        inst.state['pending']['SMC']['BTCUSDT'] = order
        inst._process_pending('SMC', 'BTCUSDT', order, 152 * HOUR,
                              high=101.0, low=95.0, open_price=101.0)
        assert said and '72' in said[0], said
