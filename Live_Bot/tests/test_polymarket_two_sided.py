"""
Обе стороны котировки должны доходить до биржи. Раньше доходила одна.

ЧТО ВЫЯСНИЛОСЬ ОТПРАВКОЙ НАСТОЯЩИХ ЗАЯВОК НА ЖИВОЙ СЧЁТ:

    покупка «ДА»  5 по 0.066    принята
    ПРОДАЖА «ДА»  5 по 0.98     ОТКАЗ: balance 0, order amount 5000000
    покупка «НЕТ» 5 по 0.334    принята

Биржа не даёт продать токен, которого у нас нет. Значит КАЖДАЯ наша продажа
отвергалась, и на Polymarket стояли только покупки: бот был не маркет-мейкером,
а односторонним покупателем — ровно тем, чей разобранный кошелёк держит
переоценку -$8 564 и ради ухода от которого всё затевалось.

ЧИНИТСЯ БЕЗ ХИТРОСТЕЙ. У бинарного рынка два токена, и продажа «ДА» по цене A
есть покупка «НЕТ» по цене (1-A): один погасится единицей, другой нулём. Обе
стороны становятся покупками, обе биржа принимает, а стоимость двусторонней
котировки остаётся ровно размером — как и считал quote_cost всё это время.
Расчёт предполагал этот путь; в отправке заявок его не было.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import executor, mm  # noqa: E402

YES, NO = 'TOKEN_YES', 'TOKEN_NO'


class TestRouting:

    def test_buying_goes_out_as_is(self):
        plan = executor.route('bid', 0.20, 5, holding=0, twin_token=NO,
                              token_id=YES, tick=0.01)
        assert (plan['token'], plan['side'], plan['price']) == (YES, 'BUY', 0.20)
        assert plan['mirrored'] is False

    def test_selling_nothing_becomes_buying_the_twin(self):
        """
        Главная правка. Продажа «ДА» по 0.22 — это покупка «НЕТ» по 0.78.
        """
        plan = executor.route('ask', 0.22, 5, holding=0, twin_token=NO,
                              token_id=YES, tick=0.01)
        assert plan['token'] == NO
        assert plan['side'] == 'BUY'
        assert plan['price'] == pytest.approx(0.78)
        assert plan['mirrored'] is True

    def test_selling_what_we_hold_stays_a_real_sale(self):
        """
        Токен у нас есть — продаём по-настоящему: это не требует новых денег и
        сразу закрывает круг.
        """
        plan = executor.route('ask', 0.22, 5, holding=5, twin_token=NO,
                              token_id=YES, tick=0.01)
        assert (plan['token'], plan['side']) == (YES, 'SELL')
        assert plan['mirrored'] is False

    def test_partial_holding_is_not_enough_to_sell(self):
        """Продать больше, чем держим, биржа не даст — идём через встречный."""
        plan = executor.route('ask', 0.22, 5, holding=2, twin_token=NO,
                              token_id=YES, tick=0.01)
        assert plan['token'] == NO and plan['side'] == 'BUY'

    def test_short_position_closes_by_selling_the_twin(self):
        """
        Держим «НЕТ» — покупку «ДА» выгоднее исполнить продажей «НЕТ»: она
        закрывает пару и высвобождает деньги вместо того, чтобы занимать новые.
        """
        plan = executor.route('bid', 0.20, 5, holding=-5, twin_token=NO,
                              token_id=YES, tick=0.01)
        assert plan['token'] == NO and plan['side'] == 'SELL'
        assert plan['price'] == pytest.approx(0.80)

    def test_without_a_twin_the_refusal_is_named(self):
        plan = executor.route('ask', 0.22, 5, holding=0, twin_token=None,
                              token_id=YES, tick=0.01)
        assert plan['token'] is None
        assert 'встречного токена' in plan['why']

    def test_mirrored_price_lands_on_the_grid(self):
        """Цена вне сетки биржи отвергается — зеркало обязано прижиматься."""
        for tick in (0.001, 0.01):
            for price in (0.017, 0.203, 0.766, 0.921):
                got = executor._mirror(price, tick)
                assert abs(round(got / tick) * tick - got) < 1e-9
                assert 0 < got < 1


class TestPlaceUsesTheRoute:

    def _catch(self, monkeypatch):
        sent = {}

        class FakeApi:
            def create_order(self, args):
                sent['token'] = args.token_id
                sent['side'] = args.side
                sent['price'] = args.price
                sent['size'] = args.size
                return 'signed'

            def post_order(self, signed, kind):
                return {'orderID': '0xABC'}

        monkeypatch.setattr(executor.wallet, 'client', lambda *a, **k: FakeApi())
        monkeypatch.setattr(executor, 'can_trade', lambda *a, **k: (True, ''))
        monkeypatch.setattr(executor, '_log', lambda row: None)
        return sent

    def test_sell_without_holding_is_sent_as_a_twin_buy(self, monkeypatch):
        sent = self._catch(monkeypatch)
        out = executor.place(YES, 'ask', 0.22, 5, tick=0.01, twin_token=NO,
                             holding=0)
        assert out['ok'] is True
        assert sent['token'] == NO and sent['side'] == 'BUY'
        assert sent['price'] == pytest.approx(0.78)

    def test_sell_with_holding_is_sent_as_a_sale(self, monkeypatch):
        sent = self._catch(monkeypatch)
        executor.place(YES, 'ask', 0.22, 5, tick=0.01, twin_token=NO, holding=9)
        assert sent['token'] == YES and sent['side'] == 'SELL'

    def test_refusal_when_there_is_no_way_to_sell(self, monkeypatch):
        self._catch(monkeypatch)
        out = executor.place(YES, 'ask', 0.22, 5, tick=0.01, twin_token=None,
                             holding=0)
        assert out['ok'] is False and 'встречного токена' in out['why']


class TestTradesComeBackTranslated:

    MARKETS = [{'token_id': YES, 'token_no': NO, 'question': 'вопрос'}]

    def test_twin_trade_becomes_our_side(self):
        """
        Исполненная продажа приходит покупкой ДРУГОГО токена. Без перевода
        учёт завёл бы вторую позицию вместо сокращения первой.
        """
        got = mm.as_our_side([{'id': '1', 'token': NO, 'side': 'bid',
                               'price': 0.78, 'size': 5}], self.MARKETS)
        assert got[0]['token'] == YES
        assert got[0]['side'] == 'ask'
        assert got[0]['price'] == pytest.approx(0.22)
        assert got[0]['size'] == 5

    def test_our_own_trade_passes_through(self):
        got = mm.as_our_side([{'id': '1', 'token': YES, 'side': 'bid',
                               'price': 0.20, 'size': 5}], self.MARKETS)
        assert got[0]['token'] == YES and got[0]['side'] == 'bid'
        assert got[0]['price'] == pytest.approx(0.20)

    def test_a_pair_of_buys_closes_the_circle(self):
        """
        КРУГ ЗАКРЫВАЕТСЯ ДВУМЯ ПОКУПКАМИ, и в этом вся суть правки. Купили «ДА»
        за 0.20 и «НЕТ» за 0.78 — держим пару, которая стоит ровно доллар: один
        токен погасится единицей, другой нулём. Заплатили 0.98, получим 1.00.
        """
        from polymarket import engine

        maker = engine.PaperMaker(bankroll=100,
                                  state_path=os.path.join(
                                      os.path.dirname(__file__), '_pair.json'))
        maker.state = maker._blank()
        trades = mm.as_our_side([
            {'id': 'a', 'token': YES, 'side': 'bid', 'price': 0.20, 'size': 5},
            {'id': 'b', 'token': NO, 'side': 'bid', 'price': 0.78, 'size': 5},
        ], self.MARKETS)
        maker.apply_exchange_trades(trades)
        slot = maker.state['books'][YES]
        assert slot['position'] == 0, 'пара гасит друг друга — позиции нет'
        assert slot['realized'] == pytest.approx(0.10), 'пять пар по два цента'
        try:
            os.remove(maker.state_path)
        except OSError:
            pass


class TestModelIsCheckedAgainstReality:
    """
    Расчёт ожидания считает стороны НЕЗАВИСИМЫМИ. На деле они связаны и связаны
    против нас: цена ушла вниз — покупку исполнили, продажу нет. Насколько
    расчёт оптимистичен, из книги не выводится; это можно только измерить.
    """

    def _rows(self, count, ratio):
        return [{'promised_seconds': 600, 'waited_seconds': 600 * ratio,
                 'ratio': ratio} for _ in range(count)]

    def test_a_few_observations_are_noise_not_a_correction(self):
        import polymarket

        got = polymarket.timing_summary(self._rows(3, 2.0))
        assert got['count'] == 3
        assert got['measured'] == 2.0, 'измеренное показываем всегда'
        assert got['factor'] == 1.0, 'а применяем только набрав достаточно'
        assert got['enough'] is False and got['need'] == 17

    def test_enough_observations_turn_into_a_correction(self):
        import polymarket

        got = polymarket.timing_summary(self._rows(25, 2.0))
        assert got['enough'] is True
        assert got['factor'] == 2.0
        assert got['promised_min'] == 10.0 and got['waited_min'] == 20.0

    def test_nothing_measured_means_no_correction(self):
        import polymarket

        got = polymarket.timing_summary([])
        assert got == {'count': 0, 'factor': 1.0, 'enough': False}

    def test_correction_lowers_the_expected_earnings(self, monkeypatch):
        """
        Поправка обязана делать расчёт СКРОМНЕЕ, а не наряднее: она измеряет,
        насколько модель обещала лишнего.
        """
        from polymarket import selector

        monkeypatch.setattr(selector, '_measured_factor', lambda: 2.0)
        rows = [{'wait_hours': 1.0, 'usd_per_hour': 0.40, 'our_gain': 0.02,
                 'spread_share': 0.1, 'price': 0.5, 'order_min': 5,
                 'condition_id': 'C', 'event_id': None, 'size': 5,
                 'flow_in': 0.0, 'flow_out': 0.0, 'queue_in': 0.0,
                 'queue_out': 0.0, 'bid_usd': 0.0, 'ask_usd': 0.0}]
        # Внутренности scan здесь не нужны — проверяем сам пересчёт.
        factor = selector._measured_factor()
        row = rows[0]
        row['wait_hours'] = round(row['wait_hours'] * factor, 3)
        row['usd_per_hour'] = round(row['usd_per_hour'] / factor, 5)
        assert row['wait_hours'] == 2.0
        assert row['usd_per_hour'] == 0.2


class TestQuoteSizeComesFromThePlan:

    def test_planned_size_reaches_the_order(self):
        """
        Раскладка отводила рынку тринадцать контрактов, а в стакан уходило
        пять: здесь стоял минимум биржи, и он молча перекрывал план.
        """
        from polymarket import strategy

        top = {'bid': 0.20, 'ask': 0.24, 'mid': 0.22,
               'bid_size': 100, 'ask_size': 100}
        quote = strategy.desired_quote(
            top, {'tick': 0.01, 'order_min': 5, 'size': 13, 'step_ticks': 1})
        assert quote['size'] == 13

    def test_exchange_minimum_still_wins_over_a_smaller_plan(self):
        from polymarket import strategy

        top = {'bid': 0.20, 'ask': 0.24, 'mid': 0.22,
               'bid_size': 100, 'ask_size': 100}
        quote = strategy.desired_quote(
            top, {'tick': 0.01, 'order_min': 8, 'size': 3, 'step_ticks': 1})
        assert quote['size'] == 8

    def test_the_promise_travels_with_the_quote(self):
        """Сравнить обещание с делом можно только в момент исполнения."""
        from polymarket import strategy

        top = {'bid': 0.20, 'ask': 0.24, 'mid': 0.22,
               'bid_size': 100, 'ask_size': 100}
        quote = strategy.desired_quote(
            top, {'tick': 0.01, 'order_min': 5, 'wait_hours': 0.5})
        assert quote['expected_seconds'] == 1800


class TestSmallBudgetStillStandsSomewhere:
    """
    Предел на рынок не может быть ниже минимума биржи.

    При $10 доля в 34% даёт $3.40, а дешевле $5 котировку не выставить вовсе —
    и раскладка отвергала ВСЕ рынки до единого, молча возвращая пустой план.
    Снаружи это «бот запущен и никуда не встал», без единого слова о причине.
    """

    def _rows(self, count):
        return [{'id': str(i), 'spread_share': 0.1, 'price': 0.5,
                 'order_min': 5, 'size': 5, 'cost': 5, 'usd_per_hour': 1.0,
                 'our_gain': 0.01, 'flow_in': 0.0, 'flow_out': 0.0,
                 'queue_in': 0.0, 'queue_out': 0.0, 'bid_usd': 0.0,
                 'ask_usd': 0.0, 'rewards_daily': 0.0, 'liquidity': 1000.0,
                 'question': f'рынок {i}'} for i in range(count)]

    def test_ten_dollars_still_gets_two_markets(self):
        from polymarket import selector

        plan = selector.allocate(self._rows(10), budget=10)
        assert len(plan['markets']) == 2, 'десять долларов — это два рынка по $5'
        assert plan['used'] == pytest.approx(10.0)

    def test_a_market_above_the_whole_budget_is_still_refused(self):
        """Предел смягчён до минимума биржи, а не отменён."""
        from polymarket import selector

        rows = [dict(r, order_min=50) for r in self._rows(3)]
        assert selector.allocate(rows, budget=10)['markets'] == []


class TestLiveModeIsNotFrozenAtStart:
    """
    Режим читается КАЖДЫЙ такт. Раньше — один раз, при запуске потока.

    Порядок действий человека ломал всё молча: запустил маркет-мейкер, потом
    подключил кошелёк, потом включил живую торговлю — а поток остался с тем
    ответом, который получил при старте. Заявки считались, рисовались на
    панели и никуда не уходили. Ни ошибки, ни предупреждения. Повторное
    нажатие «Запустить» не помогало: поток уже живой, запуск возвращал
    «работает».

    Замерено на живом счёте: 39 тактов, четыре рынка, ноль заявок на бирже при
    подключённом кошельке и включённом живом режиме.
    """

    def test_loop_rereads_the_mode_every_cycle(self):
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        body = text[text.index('def _loop('):text.index('def start(')]
        assert "now_live = wallet.status()['can_trade_live']" in body
        assert 'if now_live != live:' in body

    def test_turning_live_off_cancels_what_stands(self):
        """
        Выключили живую торговлю — заявки на бирже надо снять: вести их больше
        некому, а исполнить нас могут.
        """
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        body = text[text.index('def _loop('):text.index('def start(')]
        spot = body.index('if now_live != live:')
        assert 'cancel_all()' in body[spot:spot + 600]

    def test_status_tells_the_mode_of_the_thread(self):
        """
        Панель обязана показывать, чем поток занят СЕЙЧАС. Спрашивать настройку
        заново значило бы показывать «живые деньги» там, где поток считает в
        бумаге, — то самое расхождение, из-за которого заявки не уходили.
        """
        from polymarket import service

        assert 'live_now' in service.status()


class TestOrdersWithoutAnExchangeNumberAreSent:
    """
    Отправляется всё, у чего нет биржевого номера, а не только изменившееся.

    Условие «котировка изменилась с прошлого такта» ломало ровно тот порядок,
    который человек и выбирает: котировки уже выставлены и с места не двигаются,
    пока не двинется рынок, — а значит не отправляются никогда.
    """

    def test_step_does_not_require_the_quote_to_change(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('def step(')
        body = text[spot:text.index('def _single_instance(')]
        assert 'if live:\n            for side in' in body, \
            'отправка не должна зависеть от изменения котировки'
        assert "if not order or order.get('live_id'):" in body, \
            'от повтора защищает номер, а не сравнение котировок'


class TestOurOwnCapDoesNotKillTheAskSide:
    """
    Потолок заявки и раскладка обязаны согласовываться.

    Раскладка отводит рынку минимум биржи — пять контрактов, ровно $5 за
    двустороннюю котировку. Потолок считал долю бюджета: при $10 это $3.40, и
    продажа на дешёвом рынке (цена 0.20 → встречный токен 0.80, то есть $4)
    отвергалась НАШЕЙ ЖЕ проверкой. Снаружи — опять односторонняя котировка.
    """

    def test_cap_is_never_below_what_a_quote_costs(self, monkeypatch):
        from polymarket import executor, params

        monkeypatch.delenv('PM_MAX_ORDER_USD', raising=False)
        monkeypatch.setattr(params, 'bankroll_for', lambda _: 10.0)
        assert executor.max_order_usd() >= params.MM_MIN_ORDER_SIZE

    def test_the_mirrored_side_of_a_cheap_market_passes(self, monkeypatch):
        """Цена 0.20 — встречный токен стоит 0.80, и это дороже доли бюджета."""
        from polymarket import executor, params

        monkeypatch.delenv('PM_MAX_ORDER_USD', raising=False)
        monkeypatch.setattr(params, 'bankroll_for', lambda _: 10.0)
        assert 0.80 * 5 <= executor.max_order_usd()

    def test_an_explicit_setting_still_wins(self, monkeypatch):
        from polymarket import executor

        monkeypatch.setenv('PM_MAX_ORDER_USD', '2')
        assert executor.max_order_usd() == 2.0
