"""
Панель обязана показывать БИРЖУ, а не только собственный расчёт.

ЖАЛОБА: «деньги на полимаркете, а там пишет что сделок нет — при том что
система сделки открывает. Почему и с какого источника она берёт данные».

Вопрос был совершенно правильный, и ответ оказался хуже, чем недоразумение.

    ЗАПРОС ЗАЯВОК У БИРЖИ БЫЛ СЛОМАН. Клиент второго поколения не знает имени
    get_orders — у него get_open_orders. Вызов падал, исключение глоталось, и
    функция возвращала None. Следствие серьёзнее опечатки: сверка с биржей
    получала None и молча пропускалась КАЖДЫЙ такт. Бот ни разу не проверил,
    существуют ли его заявки на самом деле.

    ПАНЕЛЬ ПОКАЗЫВАЛА БУМАЖНУЮ МОДЕЛЬ. «Стоим на рынках 3» бралось из
    состояния расчёта и выглядело бы точно так же, даже если бы ни одна заявка
    до биржи не дошла. Причина отказа при отправке записывалась в заявку и не
    читалась никем.

Отсюда правило этой панели: слева факт от биржи, справа расчёт бота, и
подписаны они именно так.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import polymarket  # noqa: E402
from polymarket import executor, wallet  # noqa: E402


class FakeClient:
    """Клиент биржи, отвечающий как настоящий второй."""

    def __init__(self, orders=(), trades=(), fail=None):
        self._orders = list(orders)
        self._trades = list(trades)
        self._fail = fail
        self.asked = []

    def get_open_orders(self):
        self.asked.append('get_open_orders')
        if self._fail:
            raise self._fail
        return self._orders

    def get_trades(self, _params=None):
        self.asked.append('get_trades')
        return self._trades


class TestOpenOrdersAsksTheRightMethod:

    def test_uses_get_open_orders(self, monkeypatch):
        """У клиента второго поколения нет get_orders — и не было никогда."""
        api = FakeClient(orders=[{'id': '1'}, {'id': '2'}])
        monkeypatch.setattr(wallet, 'client', lambda *a, **k: api)
        rows = executor.open_orders()
        assert rows is not None and len(rows) == 2
        assert api.asked == ['get_open_orders']

    def test_failure_is_written_down_not_swallowed(self, monkeypatch):
        """
        Прежде отказ пропадал молча, и сверка отключалась навсегда без следа.
        """
        api = FakeClient(fail=AttributeError('object has no attribute get_orders'))
        monkeypatch.setattr(wallet, 'client', lambda *a, **k: api)
        written = []
        monkeypatch.setattr(executor, '_log', written.append)
        assert executor.open_orders() is None
        assert written and written[0]['action'] == 'ASK_FAILED'

    def test_reconcile_works_once_the_question_is_asked(self, monkeypatch):
        api = FakeClient(orders=[{'id': 'A'}, {'id': 'B'}])
        monkeypatch.setattr(wallet, 'client', lambda *a, **k: api)
        check = executor.reconcile({'A': ('t', 'bid', 0.5), 'C': ('t', 'ask', 0.6)})
        assert check['ghost'] == ['C'], 'у нас есть, у биржи нет'
        assert check['orphan'] == ['B'], 'у биржи есть, у нас нет'


class TestExchangeView:

    def setup_method(self):
        polymarket._EXCHANGE_CACHE.update({'at': 0.0, 'view': None})

    def test_reports_balance_orders_and_trades(self, monkeypatch):
        # Сделка приходит МЭТЧЕМ ЦЕЛИКОМ: верхние поля тейкера, наша доля
        # внутри maker_orders. Считать верхний уровень своим — та самая
        # ошибка, что показывала +$290 при минусе на счёте.
        api = FakeClient(orders=[{'id': '1', 'side': 'BUY', 'price': '0.5',
                                  'original_size': '5', 'size_matched': '0',
                                  'asset_id': 'T'}],
                         trades=[{'id': 't1', 'asset_id': 'ЧУЖОЙ',
                                  'side': 'BUY', 'price': '0.9',
                                  'size': '999', 'match_time': '1700000000',
                                  'status': 'CONFIRMED',
                                  'maker_address': '0xЧУЖОЙ',
                                  'maker_orders': [
                                      {'order_id': 'o1', 'maker_address': '0xAAA',
                                       'matched_amount': '5', 'price': '0.5',
                                       'side': 'BUY', 'asset_id': 'T'}]}])
        monkeypatch.setattr(wallet, 'funder', lambda: '0xAAA')
        monkeypatch.setattr(wallet, 'client', lambda *a, **k: api)
        monkeypatch.setattr(wallet, 'balance', lambda: 42.42)
        monkeypatch.setattr(wallet, 'status', lambda: {
            'configured': True, 'address': '0xAAA', 'funder': '0xAAA',
            'live_enabled': True, 'can_trade_live': True})
        view = polymarket.exchange_view(force=True)
        assert view['asked'] is True
        assert view['balance'] == 42.42
        assert view['orders'] == 1
        assert view['trades'] == 1
        assert view['orders_detail'][0]['side'] == 'bid'

    def test_says_why_when_it_cannot_ask(self, monkeypatch):
        monkeypatch.setattr(wallet, 'status', lambda: {
            'configured': False, 'address': None, 'funder': None,
            'live_enabled': False, 'can_trade_live': False})
        view = polymarket.exchange_view(force=True)
        assert view['asked'] is False
        assert 'кошелёк не подключён' in view['why']

    def test_answer_is_cached(self, monkeypatch):
        """Панель обновляется каждые секунды; биржу столько раз не спрашивают."""
        api = FakeClient()
        monkeypatch.setattr(wallet, 'client', lambda *a, **k: api)
        monkeypatch.setattr(wallet, 'balance', lambda: 1.0)
        monkeypatch.setattr(wallet, 'status', lambda: {
            'configured': True, 'address': '0xAAA', 'funder': '0xAAA',
            'live_enabled': True, 'can_trade_live': True})
        polymarket.exchange_view(force=True)
        first = len(api.asked)
        for _ in range(5):
            polymarket.exchange_view()
        assert len(api.asked) == first, 'повторные вопросы бирже не задаются'

    def test_snapshot_never_falls_over_a_closed_network(self, monkeypatch):
        def boom():
            raise OSError('сеть закрыта')

        monkeypatch.setattr(polymarket, 'exchange_view', boom)
        view = polymarket._exchange_safely()
        assert view['asked'] is False and 'OSError' in view['why']


class TestStandingRowKnowsWhereItStands:

    def _books(self, **order):
        base = {'price': 0.5, 'size': 5, 'ts': 0, 'queue': 0}
        return {'T': {'position': 0, 'orders': {'bid': {**base, **order},
                                                'ask': None}}}

    def test_accepted_order_carries_its_exchange_id(self):
        rows = polymarket._standing_quotes(self._books(live_id='0xABC'), [])
        assert rows[0]['live_ids'] == 1
        assert rows[0]['live_error'] is None

    def test_refused_order_carries_the_reason(self):
        """
        Причина отказа записывалась и раньше — и не читалась никем. Строка
        выглядела одинаково и когда заявка стоит на бирже, и когда её нет.
        """
        rows = polymarket._standing_quotes(
            self._books(live_error='биржа отвергла: not enough balance'), [])
        assert rows[0]['live_ids'] == 0
        assert 'not enough balance' in rows[0]['live_error']

    def test_paper_order_claims_nothing(self):
        rows = polymarket._standing_quotes(self._books(), [])
        assert rows[0]['live_ids'] == 0 and rows[0]['live_error'] is None
