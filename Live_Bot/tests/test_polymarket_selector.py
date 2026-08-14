"""
Отбор рынков для котирования. Здесь живёт защита от главной ловушки разбора.

ЛОВУШКА ВСПЛЫВАЛА ТРИЖДЫ, И ВСЯКИЙ РАЗ ПО-НОВОМУ. Как только рынок отбирался
по одному числу, наверх поднимались ПУСТЫЕ книги:

    по награде к ликвидности   бидов на $2,  асков на $15,     спред 0.889
    по относительному спреду   бидов на $0,  асков на $11 849, спред 200%
    по ширине спреда           бид 0.14 против аска 0.70 — никто не котирует
                               теснее не по щедрости, а потому что исход неясен

Второй случай особенно поучителен. Дешёвый лонгшот НИКТО не хочет покупать по
два цента, зато многие хотят продать. Наш бид оказался бы единственным: нас
исполнят немедленно, и мы получим ровно тот хвост дешёвых позиций, на котором
разобранный кошелёк держит переоценку -$8 564 при 22% позиций в плюсе.

Отсюда все проверки ниже: отбор смотрит НАСТОЯЩИЙ стакан, требует денег с обеих
сторон и меряет глубину в долларах, а не в контрактах.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import params, selector  # noqa: E402


def _market(daily=10.0, liquidity=5000.0, price=0.5, spread=0.02,
            volume=100_000, order_min=5):
    return {
        'id': 'M', 'question': 'вопрос', 'conditionId': 'C',
        'clobTokenIds': json.dumps(['TOKEN']),
        'outcomePrices': json.dumps([str(price), str(round(1 - price, 4))]),
        'clobRewards': [{'rewardsDailyRate': daily}],
        'rewardsMinSize': 20, 'rewardsMaxSpread': 4.5,
        'liquidity': liquidity, 'spread': spread, 'volume': volume,
        'orderMinSize': order_min, 'orderPriceMinTickSize': 0.01,
        'endDate': '2027-01-01T00:00:00Z',
    }


class TestQuoteCost:

    def test_both_sides_together_equal_the_size(self):
        """
        Двусторонняя котировка стоит РОВНО размер, при любой цене.

        Покупка берёт p за контракт, продажа — (1-p): продавать надо то, чего у
        нас нет. В сумме единица. Отсюда и вся арифметика малого счёта: пять
        контрактов стоят $5, и сотня долларов — это двадцать рынков, а не пять.
        """
        for price in (0.02, 0.1, 0.5, 0.9):
            assert selector.quote_cost(5, price) == pytest.approx(5.0)


class TestScanLooksAtTheRealBook:

    def _scan(self, monkeypatch, markets, budget=100, book=None):
        pages = [markets, []]
        monkeypatch.setattr(selector.client, '_get',
                            lambda url: pages.pop(0) if pages else [])
        monkeypatch.setattr(selector.params, 'PAUSE', 0)
        # Стакан подставляется: отбор смотрит настоящую книгу, и без неё ни
        # один рынок не проходит — именно этого мы и добивались.
        # Спред в ТРИ тика: ровно столько нужно, чтобы шагнуть внутрь с обеих
        # сторон и что-то оставить себе. При двух тиках отбор откажет — и это
        # проверяется отдельно ниже.
        default = {'bids': [(0.49, 2000.0)], 'asks': [(0.52, 2000.0)]}
        monkeypatch.setattr(selector.book_mod, 'fetch_many',
                            lambda tokens: {str(t): (book or default)
                                            for t in tokens})
        return selector.scan(budget=budget, pages=2)

    def test_two_sided_book_is_accepted(self, monkeypatch):
        rows = self._scan(monkeypatch, [_market()])
        assert len(rows) == 1
        assert rows[0]['size'] == 5, 'размер минимальный, допустимый биржей'
        assert rows[0]['cost'] == pytest.approx(5.0)

    def test_empty_bid_side_is_rejected(self, monkeypatch):
        """
        Односторонний стакан отсеивается, каким бы заманчивым ни выглядел.

        Дешёвый лонгшот с бидами на ноль и асками на одиннадцать тысяч — не
        возможность: наш бид окажется единственным, его снимут немедленно, и
        останется хвост, идущий к нулю.
        """
        one_sided = {'bids': [(0.02, 100.0)], 'asks': [(0.30, 400_000.0)]}
        assert self._scan(monkeypatch, [_market()], book=one_sided) == []

    def test_depth_is_measured_in_money_not_contracts(self, monkeypatch):
        """Тысяча контрактов по 0.002 — это два доллара, а не глубина."""
        thin = {'bids': [(0.002, 1000.0)], 'asks': [(0.004, 1000.0)]}
        assert self._scan(monkeypatch, [_market()], book=thin) == []

    def test_market_without_rewards_is_still_taken(self, monkeypatch):
        """
        Награда НЕ обязательна: доход даёт спред, награда идёт сверх.

        Прежняя версия брала только рынки с наградой и оставляла пять штук на
        сотню долларов. Разобранный кошелёк живёт не этим: 1 495 сделок в сутки
        на разнице цен.
        """
        assert len(self._scan(monkeypatch, [_market(daily=0.0)])) == 1

    def test_market_without_turnover_is_rejected(self, monkeypatch):
        """Стакан можно нарисовать; оборот — нет."""
        assert self._scan(monkeypatch, [_market(volume=10)]) == []

    def test_market_near_resolution_is_rejected(self, monkeypatch):
        """
        Рынок накануне разрешения не котируем — это самый крупный риск затеи.

        Мейкер зарабатывает полтора цента на контракте, а разрешение делает
        контракт нулём или единицей ЦЕЛИКОМ. Запас в пять контрактов по 0.20,
        застигнутый разрешением не в ту сторону, стоит доллар — тринадцать
        удачных кругов. Наклон против запаса разгружает за часы, а перед самым
        разрешением разгружать уже не у кого: ликвидность уходит первой.
        """
        soon = datetime.now(timezone.utc) + timedelta(hours=5)
        market = dict(_market(), endDate=soon.strftime('%Y-%m-%dT%H:%M:%SZ'))
        assert self._scan(monkeypatch, [market]) == []

    def test_market_far_from_resolution_is_kept(self, monkeypatch):
        far = datetime.now(timezone.utc) + timedelta(days=90)
        market = dict(_market(), endDate=far.strftime('%Y-%m-%dT%H:%M:%SZ'))
        assert len(self._scan(monkeypatch, [market])) == 1

    def test_missing_date_does_not_silently_drop_the_market(self):
        """Отсутствие даты — не «ноль часов», иначе отсеялось бы всё подряд."""
        assert selector._hours_left(None) == float('inf')
        assert selector._hours_left('не дата') == float('inf')

    def test_too_narrow_spread_is_rejected(self, monkeypatch):
        """Спред в один тик не покроет даже проскальзывания."""
        narrow = {'bids': [(0.4999, 5000.0)], 'asks': [(0.5001, 5000.0)]}
        assert self._scan(monkeypatch, [_market()], book=narrow) == []

    def test_two_tick_spread_is_rejected_because_we_cannot_step_inside(
            self, monkeypatch):
        """
        Двух тиков мало: шаг внутрь с обеих сторон сводит цены вместе.

        Условие здесь ТО ЖЕ, что в стратегии, и это намеренно. Пока его тут не
        было, отбор обещал 90 рынков и $450 вложений, а котировалось 22:
        стратегия отказывалась вставать в конец очереди на остальных 68. План
        расходился с делом ровно вчетверо.
        """
        two_ticks = {'bids': [(0.49, 2000.0)], 'asks': [(0.51, 2000.0)]}
        assert self._scan(monkeypatch, [_market()], book=two_ticks) == []

    def test_too_wide_spread_is_rejected(self, monkeypatch):
        """
        Слишком широкий спред — тоже отказ, и это не осторожность.

        Бид 0.14 против аска 0.70 означает, что никто не котирует теснее не по
        щедрости, а потому что исход неясен. Обе наши заявки исполнятся там
        только тогда, когда кому-то станет ясно, куда идёт цена.
        """
        wide = {'bids': [(0.14, 3000.0)], 'asks': [(0.70, 3000.0)]}
        assert self._scan(monkeypatch, [_market()], book=wide) == []


class TestAllocationSpreadsRisk:

    def _rows(self, count, cost_each=5.0, spread_share=0.1):
        return [{'id': str(i), 'cost': cost_each, 'spread_share': spread_share,
                 'rewards_daily': 0.0, 'liquidity': 1000.0,
                 'question': f'рынок {i}'} for i in range(count)]

    def test_budget_spreads_over_many_markets(self, monkeypatch):
        """
        Сотня долларов при пяти контрактах — это двадцать рынков.

        Ровно то, чего требовала исходная задача: работать на многих мелких
        событиях, а не на нескольких крупных.
        """
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.34)
        plan = selector.allocate(self._rows(50, cost_each=5.0), budget=100)
        assert len(plan['markets']) == 20
        assert plan['used'] == pytest.approx(100.0)

    def test_one_market_never_takes_the_whole_budget(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 0.34)
        plan = selector.allocate(self._rows(1, cost_each=100.0), budget=100)
        assert plan['markets'] == []

    def test_budget_is_never_exceeded(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate(self._rows(40, cost_each=30.0), budget=100)
        assert plan['used'] <= 100.0

    def test_ceiling_is_reported_as_a_ceiling(self, monkeypatch):
        """
        Потолок показывается числом и называется потолком.

        Он предполагает, что обе стороны исполнились по нашим ценам и ни разу
        не против нас. Назвать его ожиданием значило бы выдать арифметику за
        обещание — ни того, ни другого мы пока не наблюдали.
        """
        monkeypatch.setattr(params, 'MM_MAX_MARKET_SHARE', 1.0)
        plan = selector.allocate(self._rows(20, cost_each=5.0, spread_share=0.1),
                                 budget=100)
        # Половина спреда на вложенное: 0.1 / 2 × $100 = $5 за полный оборот.
        assert plan['ceiling_per_round_usd'] == pytest.approx(5.0)
        assert 'expected' not in str(plan), 'слово «ожидаемый» здесь неуместно'
