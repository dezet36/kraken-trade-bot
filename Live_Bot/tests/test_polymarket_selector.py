"""
Отбор рынков под размер капитала. Здесь живёт защита от главной ловушки.

ЛОВУШКА, РАДИ КОТОРОЙ ЭТОТ МОДУЛЬ И ПОЯВИЛСЯ. Награды платятся на 788 рынках,
$5 976 в день. Естественный ход — идти туда, где награда велика относительно
стоящей ликвидности: доля в пуле будет наибольшей. Проверка показала, что так
выбираются ПУСТЫЕ рынки: бидов на $2, асков на $15, спред 0.889.

Награда там не разобрана не потому, что её не заметили. Чтобы её получить, надо
стоять в пределах 3.5-5.5 цента от середины, а в рынке со спредом 0.889 это
значит выставить узкую котировку туда, где никто не торгует. Первый же, кому
что-то понадобится, снимет нас по своей цене — и на сотне долларов это конец
счёта, а не неприятность.

Поэтому главный тест здесь — что рынок с широким спредом ОТСЕИВАЕТСЯ, каким бы
привлекательным ни выглядел по награде.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import params, selector  # noqa: E402


def _market(daily=10.0, liquidity=5000.0, min_size=20, price=0.5,
            spread=0.01, max_spread=4.5):
    import json as _json
    return {
        'id': 'M', 'question': 'вопрос', 'conditionId': 'C',
        'clobTokenIds': _json.dumps(['TOKEN']),
        'outcomePrices': _json.dumps([str(price), str(round(1 - price, 4))]),
        'clobRewards': [{'rewardsDailyRate': daily}],
        'rewardsMinSize': min_size, 'rewardsMaxSpread': max_spread,
        'liquidity': liquidity, 'spread': spread,
        'orderPriceMinTickSize': 0.01, 'endDate': '2027-01-01T00:00:00Z',
    }


class TestQuoteCost:

    def test_both_sides_are_counted(self):
        """
        Двусторонняя котировка требует капитала на ОБЕ стороны.

        Покупка стоит p за контракт, продажа — (1-p): продавать надо то, чего у
        нас нет, и биржа держит залог. Считать одну сторону значило бы вдвое
        занизить потребность и обнаружить это на первом отказе.
        """
        assert selector.quote_cost(100, 0.5) == pytest.approx(100.0)
        assert selector.quote_cost(100, 0.05) == pytest.approx(100.0)
        assert selector.quote_cost(20, 0.9) == pytest.approx(20.0)


class TestScanRejectsTheTrap:

    def _scan(self, monkeypatch, markets, budget=100):
        pages = [markets, []]
        monkeypatch.setattr(selector.client, '_get',
                            lambda url: pages.pop(0) if pages else [])
        monkeypatch.setattr(selector.params, 'PAUSE', 0)
        monkeypatch.setattr(selector.params, 'MM_MIN_LIQUIDITY', 1000)
        return selector.scan(budget=budget, pages=2)

    def test_wide_spread_market_is_rejected(self, monkeypatch):
        """
        Спред шире порога награды — рынок не берём, как бы ни манила награда.

        Это ровно та ловушка: чтобы попасть в зачёт, пришлось бы встать узко
        там, где никто не торгует, и первый же встречный снял бы нас по своей
        цене.
        """
        rich_but_dead = _market(daily=500.0, liquidity=2000.0, spread=0.889)
        assert self._scan(monkeypatch, [rich_but_dead]) == []

    def test_tight_spread_market_is_accepted(self, monkeypatch):
        """Спред уже порога — встать у лучшей цены безопасно и зачётно."""
        rows = self._scan(monkeypatch, [_market(spread=0.01, max_spread=4.5)])
        assert len(rows) == 1

    def test_empty_book_is_rejected(self, monkeypatch):
        """Пустой стакан — не возможность, а предупреждение."""
        assert self._scan(monkeypatch, [_market(liquidity=50.0)]) == []

    def test_market_without_rewards_is_rejected(self, monkeypatch):
        assert self._scan(monkeypatch, [_market(daily=0.0)]) == []

    def test_quote_too_expensive_for_the_budget_is_rejected(self, monkeypatch):
        """
        Минимальный размер не по карману — рынок бесполезен.

        Меньше порога заявка не считается вовсе: мы стояли бы в стакане, неся
        риск, и не получали за это ничего.
        """
        assert self._scan(monkeypatch, [_market(min_size=500)], budget=100) == []

    def test_expected_daily_falls_as_the_pool_gets_crowded(self, monkeypatch):
        """Чем больше уже стоит в стакане, тем меньше наша доля."""
        thin = self._scan(monkeypatch, [_market(liquidity=2000.0)])[0]
        monkeypatch.undo()
        crowded = self._scan(monkeypatch, [_market(liquidity=200000.0)])[0]
        assert thin['expected_daily'] > crowded['expected_daily']


class TestAllocationSpreadsRisk:

    def _rows(self, count, cost_each=20.0, daily=0.1):
        return [{'id': str(i), 'cost': cost_each, 'expected_daily': daily,
                 'question': f'рынок {i}'} for i in range(count)]

    def test_one_market_never_takes_the_whole_budget(self, monkeypatch):
        """
        Предел на рынок — следствие арифметики малого счёта, а не осторожности.

        Без него сотня долларов уходила целиком в один рынок: самый доходный по
        оценке и единственный. Одно исполнение — и половина счёта в позиции,
        которую нечем нести и нечем усреднить.
        """
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.34)
        plan = selector.allocate(self._rows(1, cost_each=100.0), budget=100)
        assert plan['markets'] == []
        assert plan['used'] == 0.0

    def test_budget_is_spread_over_several_markets(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.34)
        plan = selector.allocate(self._rows(10, cost_each=20.0), budget=100)
        assert len(plan['markets']) == 5
        assert plan['used'] == pytest.approx(100.0)

    def test_budget_is_never_exceeded(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate(self._rows(10, cost_each=30.0), budget=100)
        assert plan['used'] <= 100.0

    def test_expected_income_is_reported_not_hidden(self, monkeypatch):
        """
        Ожидаемый доход показывается числом.

        При сотне долларов он измеряется единицами долларов в месяц, и знать
        это надо заранее, а не обнаружить через месяц.
        """
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.34)
        plan = selector.allocate(self._rows(5, cost_each=20.0, daily=0.05),
                                 budget=100)
        assert plan['expected_daily'] == pytest.approx(0.25)
        assert plan['expected_monthly'] == pytest.approx(7.5)
