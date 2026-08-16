"""
Постоянный контроль позиций.

ЧЕГО НЕ ХВАТАЛО. По списку позиций и списку заявок нельзя ответить на
единственный важный вопрос: ведётся позиция или висит мёртвым грузом. Список
говорит «держим пять по 0.637», заявки — «продаём по 0.619». Ни один не
говорит, что рынок ушёл на 0.474 и эта продажа не исполнится никогда.

Замерено на живом счёте ровно в таком виде:

    «Max Martin»              продаём 0.619, рынок 0.474 — 14 центов мимо
    «Democrats win Virginia»  впереди 4 827 контрактов, тишина 14 часов
    «Democratic Party IA-03»  встречного потока нет вовсе, тишина 33 часа

Каждая выглядела работающей заявкой. Ни одна не могла исполниться.
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import control, engine, params  # noqa: E402

CATALOGUE = {
    'YES': {'question': 'рынок', 'token_no': 'NO', 'tick': 0.01},
    'NO': {'question': 'рынок', 'token_no': 'YES', 'tick': 0.01},
}
BOOK = {'bids': [(0.20, 100.0), (0.19, 400.0)],
        'asks': [(0.22, 80.0), (0.23, 300.0)]}


def _maker(tmp_path, position=5.0, cost=0.20, age_hours=1.0):
    made = engine.PaperMaker(bankroll=100,
                             state_path=os.path.join(str(tmp_path), 's.json'))
    slot = made._slot('YES')
    slot['position'] = position
    slot['avg_cost'] = cost
    slot['opened_ts'] = time.time() - age_hours * 3600
    return made


def _review(maker, orders=None, book=None):
    return control.review(maker, {'YES': book or BOOK}, CATALOGUE,
                          orders=orders or {})


class TestEveryPositionGetsAVerdict:

    def test_a_position_without_an_order_is_named(self, tmp_path):
        got = _review(_maker(tmp_path))
        assert got[0]['verdict'] == 'без заявки'
        assert got[0]['position'] == 5.0

    def test_a_price_far_from_the_market_says_so(self, tmp_path):
        """
        Продажа дороже лучшего аска ИСПОЛНИМА — просто после тех, кто дешевле.
        Разница не в возможности, а в длине очереди, и подменять число словом
        нельзя. Но расстояние до середины называется отдельно: тридцать центов
        от неё — это уже не очередь, а другая цена.
        """
        got = _review(_maker(tmp_path),
                      orders={'YES': [{'price': 0.60, 'original_size': 5}]})
        assert got[0]['from_mid'] == pytest.approx(0.39, abs=0.01)
        assert got[0]['worse_than_best'] == pytest.approx(0.38, abs=0.01)
        assert 'от середины' in got[0]['why']

    def test_the_front_of_the_queue_is_recognised(self, tmp_path):
        """Наша цена лучше всех — исполнят следующей встречной сделкой."""
        got = _review(_maker(tmp_path),
                      orders={'YES': [{'price': 0.21, 'original_size': 5}]})
        assert got[0]['verdict'] == 'первые в очереди'
        assert got[0]['queue_ahead'] == 0.0

    def test_a_queue_is_counted_without_our_own_size(self, tmp_path):
        """Своя заявка себя не задерживает."""
        got = _review(_maker(tmp_path),
                      orders={'YES': [{'price': 0.22, 'original_size': 30}]})
        assert got[0]['queue_ahead'] == pytest.approx(50.0)

    def test_a_hopeless_queue_is_told_apart(self, tmp_path):
        """
        Впереди четыре тысячи контрактов при нашей пятёрке — это не «ждём», а
        «не дождёмся». Замерено: 4 827 впереди при тишине четырнадцать часов.
        """
        deep = {'bids': BOOK['bids'],
                'asks': [(0.22, 4827.0), (0.23, 300.0)]}
        got = _review(_maker(tmp_path), book=deep,
                      orders={'YES': [{'price': 0.22, 'original_size': 5}]})
        assert got[0]['verdict'] == 'за очередью'
        assert got[0]['queue_ahead'] == pytest.approx(4822.0)

    def test_a_missing_book_is_not_silence(self, tmp_path):
        got = control.review(_maker(tmp_path), {}, CATALOGUE, orders={})
        assert got[0]['verdict'] == 'нет книги'


class TestTheTwinOrderIsFound:

    def test_a_sale_placed_as_a_twin_purchase_counts(self, tmp_path):
        """
        Продажа «ДА» уходит на биржу покупкой «НЕТ». Ища только по основному
        токену, мы объявили бы «позиция без заявки» на каждом втором рынке.
        """
        got = _review(_maker(tmp_path, position=-5.0, cost=0.20),
                      orders={'NO': [{'price': 0.79, 'original_size': 5}]})
        assert got[0]['our_price'] == pytest.approx(0.21)
        assert got[0]['verdict'] != 'без заявки'


class TestTheClockIsShown:

    def test_hours_until_the_bot_acts_are_counted(self, tmp_path):
        """
        «Застряла» без срока читается как «навсегда», а это неправда: бот сам
        выйдет через рынок по второму сроку.
        """
        got = _review(_maker(tmp_path, age_hours=2.0))
        assert got[0]['held_hours'] == pytest.approx(2.0, abs=0.05)
        assert got[0]['until_best_price'] == pytest.approx(
            params.MM_MAX_HOLD_HOURS - 2, abs=0.05)
        assert got[0]['until_market_exit'] == pytest.approx(
            params.MM_MAX_HOLD_HOURS * params.MM_DESPERATE_AFTER - 2, abs=0.05)

    def test_an_overdue_position_shows_a_negative(self, tmp_path):
        got = _review(_maker(tmp_path, age_hours=100.0))
        assert got[0]['until_market_exit'] < 0


class TestTheSummarySeparatesWorkFromWaiting:

    def test_working_and_stuck_are_not_mixed(self, tmp_path):
        maker = _maker(tmp_path)
        maker._slot('OTHER')['position'] = 5.0
        maker._slot('OTHER')['avg_cost'] = 0.5
        rows = control.review(maker, {'YES': BOOK}, CATALOGUE,
                              orders={'YES': [{'price': 0.21,
                                               'original_size': 5}]})
        got = control.summary(rows)
        assert got['positions'] == 2
        assert got['working'] == 1
        assert got['stuck'] == 1

    def test_the_worst_comes_first(self, tmp_path):
        """Панель обязана показывать застрявшее сверху, а не терять в списке."""
        maker = _maker(tmp_path)
        maker._slot('OTHER')['position'] = 5.0
        rows = control.review(maker, {'YES': BOOK}, CATALOGUE,
                              orders={'YES': [{'price': 0.21,
                                               'original_size': 5}]})
        assert rows[0]['verdict'] == 'нет книги'
        assert rows[-1]['verdict'] == 'первые в очереди'

    def test_an_empty_account_says_nothing_alarming(self, tmp_path):
        maker = engine.PaperMaker(
            bankroll=100, state_path=os.path.join(str(tmp_path), 's.json'))
        got = control.summary(control.review(maker, {}, CATALOGUE))
        assert got['positions'] == 0
        assert got['stuck'] == 0


class TestTheClosingSideAsksForWhatWeHold:
    """
    ТИХАЯ И ДОРОГАЯ ОШИБКА, НАЙДЕННАЯ ПОСТОЯННЫМ КОНТРОЛЕМ.

    Частичное исполнение оставляет остаток вроде 4.9926 контракта. Продажа же
    просилась ровно на пять — а продать больше, чем держишь, биржа не даёт, и
    отправка честно превращала продажу в ПОКУПКУ встречного токена по 0.927:

        держим 4.9926 по 0.078, хотим продать 5
        уходит покупка «НЕТ» на $4.64 — денег нет, отказ каждый такт

    Снаружи это выглядело как «позиция без заявки»: выход не выставлен, и
    почему — не сказано.
    """

    def _quote(self, position, size=5, order_min=5):
        from polymarket import strategy
        top = {'bid': 0.20, 'ask': 0.30, 'mid': 0.25,
               'bid_size': 100, 'ask_size': 100}
        market = {'tick': 0.01, 'order_min': order_min, 'size': size,
                  'step_ticks': 0, 'stale': True}
        return strategy.desired_quote(top, market, position=position,
                                      avg_cost=0.20)

    def test_we_sell_only_what_we_have(self):
        got = self._quote(position=7.0, size=20)
        assert got['size'] == pytest.approx(7.0)

    def test_a_whole_position_is_sold_whole(self):
        got = self._quote(position=20.0, size=20)
        assert got['size'] == pytest.approx(20.0)

    def test_dust_below_the_exchange_minimum_is_named(self):
        """
        Заявка на 4.99 при минимуме 5 будет отвергнута. Притворяться, что
        выход выставлен, незачем — остаток называется прямо.
        """
        got = self._quote(position=4.9926)
        assert got['bid'] is None and got['ask'] is None
        assert 'меньше минимума' in got['reason']

    def test_an_opening_quote_keeps_its_size(self):
        """Ограничение касается только закрывающей стороны."""
        from polymarket import strategy
        top = {'bid': 0.20, 'ask': 0.30, 'mid': 0.25,
               'bid_size': 100, 'ask_size': 100}
        got = strategy.desired_quote(top, {'tick': 0.01, 'order_min': 5,
                                           'size': 20, 'step_ticks': 0},
                                     position=0.0, avg_cost=0.0)
        assert got['size'] == pytest.approx(20.0)


class TestAPhantomQuoteIsNotShownAsWork:
    """
    ЗАЯВКА БЕЗ БИРЖЕВОГО НОМЕРА И БЕЗ ОШИБКИ — ПРИЗРАК.

    Пропуская рынок по деньгам, мы к нему в этом такте больше не вернёмся — а
    вместе с ним и к заявке, которую успели сочинить раньше. Она остаётся в
    наших книгах без номера и без ошибки: на бирже её нет, отправлять некому,
    причины никто не записал.

    Панель показывала такую строку как «только расчёт» с подсказкой «живой
    режим выключен» — при включённом живом режиме. Замерено: девять строк из
    тридцати одной, и по каждой человек искал поломку там, где её не было.
    """

    def test_the_unsent_order_is_dropped_on_a_budget_skip(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index("skipped['бюджет исчерпан']")
        block = text[spot:spot + 1400]
        assert "if order and not order.get('live_id')" in block
        assert "slot['orders'][side] = None" in block

    def test_an_order_on_the_exchange_is_never_dropped(self):
        """Она стоит и живёт своей жизнью, хватает нам денег или нет."""
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index("skipped['бюджет исчерпан']")
        assert 'Заявку с номером не трогаем' in text[spot:spot + 1400]

    def test_the_panel_stops_claiming_live_is_off(self):
        """
        «Только расчёт» означает «живой режим выключен». При включённом это
        неправда, и подпись должна быть другой.
        """
        html = open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()
        assert 'ещё не отправлена' in html
        spot = html.index('только расчёт</span>')
        assert 'PM.service.live_now' in html[spot - 700:spot]
