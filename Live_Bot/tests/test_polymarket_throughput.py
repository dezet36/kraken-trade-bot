"""
Число сделок: почему у нас десятки, а у разобранного кошелька полторы тысячи.

    сделок в сутки = рынков × оборотов на рынке

@planktonxd делает 1 495 сделок в сутки и держит 2 236 позиций ОДНОВРЕМЕННО.
То есть он не ждёт закрытия круга — продолжает входить.

МЫ ЖЕ ПОСЛЕ ПЕРВОГО ИСПОЛНЕНИЯ ЗАМОЛКАЛИ. Вход дешёвой стороной сберёг капитал,
но переключал рынок в «только закрывать» при любой позиции: одна-две сделки в
сутки на рынок вместо десятков. При этом наши заявки исполняются за 15-20
минут — скорость была, оборота не было.

Держать обе стороны безопасно ровно потому, что есть НАКЛОН ПРОТИВ ЗАПАСА:
каждая следующая покупка идёт по цене хуже предыдущей, продажа — лучше. Запас
растёт со всё большим сопротивлением и упирается в полный наклон, где вход
закрывается сам.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import mm, params, strategy  # noqa: E402

TOP = {'bid': 0.20, 'ask': 0.24, 'mid': 0.22, 'bid_size': 100, 'ask_size': 100}
MARKET = {'tick': 0.01, 'order_min': 5, 'size': 5, 'step_ticks': 1}


def _quote(position, cost=0.21):
    return strategy.desired_quote(TOP, MARKET, position=position,
                                  avg_cost=cost if position else 0.0)


class TestTheMarketKeepsWorkingAfterAFill:

    def test_flat_enters_one_side_only(self):
        """Позиции нет — занимаем деньги только под дешёвую сторону."""
        assert _quote(0)['only'] == 'bid'

    def test_a_partial_position_quotes_both_sides(self):
        """
        Главная правка. Рынок с одной набранной котировкой продолжает
        работать: закрывающая сторона бесплатна, входная идёт под наклоном.
        """
        quote = _quote(5)
        assert quote['only'] is None

    def test_a_full_position_stops_entering(self):
        full = MARKET['size'] * params.MM_SKEW_FULL_AT
        assert _quote(full)['only'] == 'ask'
        assert _quote(full + 5)['only'] == 'ask'

    def test_each_next_entry_is_worse_than_the_last(self):
        """Наклон — то, что делает продолжение входа безопасным."""
        first = _quote(0)['bid']
        second = _quote(5)['bid']
        third = _quote(9)['bid']
        assert first > second > third

    def test_the_closing_side_gets_better_as_we_fill(self):
        assert _quote(9)['ask'] <= _quote(5)['ask'] < _quote(0)['ask']


class TestOnlyNewMoneyIsCharged:

    def test_an_entry_costs_its_own_leg(self):
        quote = _quote(0)
        assert mm._quote_needs(quote, 0) == pytest.approx(5 * quote['bid'])

    def test_closing_what_we_hold_is_free(self):
        quote = {'bid': 0.19, 'ask': 0.22, 'size': 5, 'only': 'ask'}
        assert mm._quote_needs(quote, 5) == 0.0

    def test_closing_a_short_is_free_too(self):
        quote = {'bid': 0.19, 'ask': 0.22, 'size': 5, 'only': 'bid'}
        assert mm._quote_needs(quote, -5) == 0.0

    def test_two_sides_charge_only_the_entry(self):
        """Держим пять: продажа бесплатна, платим лишь за докупку."""
        quote = {'bid': 0.20, 'ask': 0.22, 'size': 5, 'only': None}
        assert mm._quote_needs(quote, 5) == pytest.approx(1.0)

    def test_two_sides_with_no_position_cost_the_full_size(self):
        """Нечего продавать — обе стороны требуют денег."""
        quote = {'bid': 0.20, 'ask': 0.22, 'size': 5, 'only': None}
        assert mm._quote_needs(quote, 0) == pytest.approx(5 * 0.20 + 5 * 0.78)

    def test_a_partial_holding_does_not_count_as_free(self):
        """Продать больше, чем держим, биржа не даст — деньги понадобятся."""
        quote = {'bid': 0.20, 'ask': 0.22, 'size': 5, 'only': 'ask'}
        assert mm._quote_needs(quote, 2) > 0


class TestInventoryStaysBounded:
    """
    Плата за оборот — запас. У разобранного кошелька он вырос до 2 236 позиций
    с переоценкой -$8 564; у нас его держат три ограничения.
    """

    def test_the_skew_closes_the_entry_by_itself(self):
        full = MARKET['size'] * params.MM_SKEW_FULL_AT
        assert _quote(full)['only'] == 'ask', 'вход закрывается сам'

    def test_the_hold_limit_still_applies(self):
        quote = strategy.desired_quote(TOP, dict(MARKET, stale=True),
                                       position=5, avg_cost=0.21)
        assert quote['only'] == 'ask'

    def test_the_loss_limit_is_still_a_number(self):
        assert 0 < params.MM_MAX_POSITION_LOSS < 1
