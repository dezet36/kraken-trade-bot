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


class TestTwoWayFlowIsRequired:
    """
    Порог на двусторонний поток. Добавлен ПОСЛЕ замера, и без него стратегия
    была бы убыточной.

    По ленте на дешёвых рынках медианная доля выходов равна НУЛЮ: продавцы по
    нашей цене входа есть, покупателей по цене выхода нет вовсе. «Milei out as
    President», «Will Arc launch a token» — по две продажи в час и ни одной
    покупки выше. Купив там, мы не вышли бы никогда и получили ровно те 2 236
    висящих позиций разобранного кошелька.

    Дешёвый лонгшот тем и дёшев, что все хотят из него выйти.
    """

    def _row(self, ins=10.0, outs=10.0, size_flow=200.0, bid=0.10):
        return {'condition_id': 'C', 'token_id': 'T', 'tick': 0.01,
                'order_min': 5, 'bid_usd': 1000.0, 'question': 'рынок',
                'top': {'bid': bid, 'ask': bid + 0.03,
                        'mid': bid + 0.015, 'spread': 0.03},
                'in_per_hour': ins, 'out_per_hour': outs,
                'in_size_per_hour': size_flow,
                'exit_share': min(1.0, outs / ins) if ins else 0.0}

    def test_one_way_market_is_rejected(self):
        assert oneside.keep_two_way([self._row(ins=10.0, outs=0.0)]) == []

    def test_two_way_market_is_kept(self):
        assert len(oneside.keep_two_way([self._row(ins=10.0, outs=8.0)])) == 1

    def test_market_without_any_entries_is_rejected(self):
        """Ни одной продажи по нашей цене — котировать нечего и незачем."""
        assert oneside.keep_two_way([self._row(ins=0.0, outs=5.0)]) == []

    def test_half_the_exits_is_the_line(self):
        """
        Порог 0.5 при арифметической нужде в 0.25 — запас вдвое.

        Замер прикладывает сегодняшний стакан ко вчерашним сделкам и потому
        приблизителен; половина вместо четверти оплачивает эту неточность.
        """
        assert oneside.keep_two_way([self._row(ins=10.0, outs=4.0)]) == []
        assert len(oneside.keep_two_way([self._row(ins=10.0, outs=5.0)])) == 1


class TestSizeFollowsTheFlow:
    """
    Размер считается от ПОТОКА, а не пятёркой на всё подряд.

    Жёсткая пятёрка оставляла 97% счёта без дела: после порога остаётся горстка
    рынков, и на них уходило $2.39 из ста. Поток продаж по нашим ценам — 299
    контрактов в час, чужая сделка бывает и в 357 контрактов.
    """

    def _row(self, flow=200.0, bid_usd=1000.0, bid=0.10):
        return {'in_size_per_hour': flow, 'bid_usd': bid_usd, 'order_min': 5,
                'tick': 0.01, 'top': {'bid': bid}}

    def test_size_grows_with_the_flow(self):
        small = oneside.size_for(self._row(flow=20.0), budget=100)
        big = oneside.size_for(self._row(flow=400.0), budget=100)
        assert big > small

    def test_flow_is_counted_in_contracts_not_trades(self):
        """
        Ошибка, пойманная на себе: поток считался в СДЕЛКАХ, и размер выходил
        впятеро меньше минимального. Сделок в час бывает 1.3, а контрактов в
        той же сделке — 357.
        """
        assert oneside.size_for(self._row(flow=300.0), budget=100) > 5

    def test_one_market_never_takes_a_large_share_of_the_account(self):
        """Разрешение одного рынка не должно быть событием для всего счёта."""
        got = oneside.size_for(self._row(flow=1e6), budget=100)
        assert got * 0.11 <= 100 * params.OS_MAX_MARKET_SHARE + 1e-6

    def test_we_never_become_most_of_the_book(self):
        """
        Быть большей частью бида значит двигать цену собой и остаться
        единственным покупателем, когда придёт продавец, который знает больше.
        """
        got = oneside.size_for(self._row(flow=1e6, bid_usd=100.0), budget=1e6)
        assert got * 0.11 <= 100.0 * params.OS_MAX_BOOK_SHARE + 1e-6

    def test_size_never_falls_below_the_exchange_minimum(self):
        assert oneside.size_for(self._row(flow=0.1), budget=100) == 5
