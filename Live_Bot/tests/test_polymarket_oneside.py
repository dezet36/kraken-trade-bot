"""
Односторонний мейкер на дешёвых рынках.

ЗДЕСЬ ЗАКРЕПЛЕНО ОДНО РЕШЕНИЕ, ОШИБКА В КОТОРОМ НЕОБРАТИМА: стороны никогда не
котируются вместе. Докупка к имеющейся позиции — ровно тот путь, которым
разобранный кошелёк набрал 2 236 позиций и переоценку -$8 564 при +$11 000
зафиксированных. Каждая покупка по отдельности выглядит дешёвой; накопление не
имеет предела во времени.

С потолком в одну партию накопление ограничено ЧИСЛОМ рынков, а не сроком
работы. Это и есть всё отличие от разобранного кейса.

ПОЛОСА ЦЕН ВЗЯТА ИЗ ЗАМЕРА, а не из удобства: на 3 001 разрешённом рынке и 375
событиях недобор в 0.02-0.15 составил +0.0191 на контракт при интервале
[-0.0034, +0.0446]. Вне полосы замера нет.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import oneside, params  # noqa: E402


def _top(bid=0.10, ask=0.13, bid_size=100, ask_size=100):
    return {'bid': bid, 'ask': ask, 'mid': round((bid + ask) / 2, 6),
            'bid_size': bid_size, 'ask_size': ask_size,
            'spread': round(ask - bid, 6)}


def _market(tick=0.01, order_min=5, avg_cost=0.0):
    return {'tick': tick, 'order_min': order_min, 'avg_cost': avg_cost}


class TestCapitalAdvantage:

    def test_one_side_costs_only_the_price(self):
        """
        Здесь вся выгода стратегии, и её стоит закрепить числом.

        Двусторонняя котировка стоит РОВНО размер при любой цене: покупка берёт
        p, продажа (1-p). Односторонняя берёт только p. На цене 0.10 это $0.50
        против $5 — вдесятеро больше рынков на те же деньги.
        """
        assert oneside.quote_cost(5, 0.10) == pytest.approx(0.50)
        assert oneside.quote_cost(5, 0.02) == pytest.approx(0.10)

    def test_hundred_dollars_covers_far_more_markets(self):
        rows = [{'cost': oneside.quote_cost(5, 0.10), 'size': 5, 'tick': 0.01,
                 'trades_per_hour': 1.0} for _ in range(400)]
        got = oneside.plan(rows, budget=100)
        assert len(got['markets']) == 200, 'вдесятеро больше двадцати'


class TestNeverAccumulates:
    """Главное ограничение риска. Ошибка здесь необратима."""

    def test_position_switches_the_quote_to_the_exit(self):
        """
        С позицией котируется ТОЛЬКО выход, никогда не докупка.

        Это единственное отличие от разобранного кошелька, и оно же —
        единственная причина, по которой накопление здесь ограничено.
        """
        got = oneside.desired_quote(_top(), _market(avg_cost=0.11), position=5)
        assert got['side'] == 'ask'

    def test_flat_position_quotes_only_the_entry(self):
        got = oneside.desired_quote(_top(), _market(), position=0)
        assert got['side'] == 'bid'

    def test_exit_is_never_below_what_we_paid(self):
        """
        Выход не ставится дешевле входа: это фиксировало бы убыток руками.

        Единственный убыток, который стратегия принимает, — разрешение рынка
        против нас. Продавать дешевле покупки значит добавлять к нему второй,
        добровольный.
        """
        got = oneside.desired_quote(_top(bid=0.05, ask=0.20),
                                    _market(avg_cost=0.12), position=5)
        assert got['price'] > 0.12

    def test_exit_size_never_exceeds_the_position(self):
        got = oneside.desired_quote(_top(), _market(avg_cost=0.11), position=3)
        assert got['size'] <= 3


class TestPriceBandComesFromMeasurement:

    def test_expensive_market_is_declined(self):
        """
        Вне полосы 0.02-0.15 замера недобора нет.

        Котировать там значило бы опираться на ничто: единственное, что мы
        знаем про накопление, измерено именно в этой полосе.
        """
        got = oneside.desired_quote(_top(bid=0.40, ask=0.44), _market(), 0)
        assert got.get('reason') and 'полос' in got['reason']

    def test_too_cheap_market_is_declined(self):
        got = oneside.desired_quote(_top(bid=0.001, ask=0.004),
                                    _market(tick=0.001), 0)
        assert got.get('reason')

    def test_band_matches_the_measured_one(self):
        assert params.OS_MIN_PRICE == 0.02
        assert params.OS_MAX_PRICE == 0.15


class TestEntryPricing:

    def test_entry_steps_inside_the_book(self):
        """Встав на лучшую цену, мы попали бы в конец чужой очереди."""
        got = oneside.desired_quote(_top(bid=0.10, ask=0.13), _market(), 0)
        assert got['price'] > 0.10

    def test_narrow_spread_is_declined(self):
        """
        Одного тика мало: нужен один на вход внутрь и один на выход выше.
        """
        got = oneside.desired_quote(_top(bid=0.10, ask=0.11), _market(), 0)
        assert got.get('reason') and 'очеред' in got['reason']

    def test_entry_never_crosses_the_market(self):
        for bid, ask in ((0.10, 0.12), (0.10, 0.13), (0.02, 0.15)):
            got = oneside.desired_quote(_top(bid, ask), _market(), 0)
            if got and not got.get('reason'):
                assert got['price'] < ask


class TestPlanNamesTheWorstCase:

    def test_worst_case_is_reported(self):
        """
        Худший случай называется прямо: если КАЖДЫЙ рынок исполнится и КАЖДЫЙ
        разрешится против нас, теряется всё вложенное.

        Замер говорит, что разрешаются они примерно по цене, но замер — не
        гарантия, и интервал накрывает ноль. Прятать это число значило бы
        выдавать измерение за обещание.
        """
        rows = [{'cost': 0.5, 'size': 5, 'tick': 0.01, 'trades_per_hour': 2.0}
                for _ in range(20)]
        got = oneside.plan(rows, budget=100)
        assert got['worst_case_usd'] == pytest.approx(10.0)
        assert got['used'] == pytest.approx(10.0)

    def test_budget_is_never_exceeded(self):
        rows = [{'cost': 0.6, 'size': 5, 'tick': 0.01, 'trades_per_hour': 1.0}
                for _ in range(500)]
        assert oneside.plan(rows, budget=100)['used'] <= 100.0
