"""
Вынос ликвидности: скопления, заглядывание вперёд и цель за забором.

ГЛАВНАЯ ОПАСНОСТЬ ЗДЕСЬ — ПИВОТЫ ИЗ БУДУЩЕГО. Пивот определяется окном
±PIVOT_BARS и становится известен лишь через PIVOT_BARS баров после себя.
Скопление, собранное без учёта этого, содержит экстремумы, которых на баре
решения ещё не было, — и замер выдаёт красивый результат, невоспроизводимый в
бою. Ошибка не падает и видна только по итоговому числу.

Второе — цель. Весь смысл сетапа в том, что она стоит ЗА противоположным
скоплением, а не на нём: стопы лежат за чертой, значит и цена тянется за
черту. Цель на самом уровне уже дважды губила замеры (сетка, коридор), и
проверка на это здесь явная.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from liquidity import core  # noqa: E402


def series(n=200, seed=5):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    return high, low, close


class TestNoLookahead:
    def test_pivot_is_known_only_after_its_window(self):
        high, low, close = series()
        for index, _price, _side, known_at in core.pivots(high, low, bars=3):
            assert known_at == index + 3
            assert known_at > index

    def test_pools_ignore_unconfirmed_pivots(self):
        """
        Скопление на баре `at` не может опираться на пивот, окно которого ещё
        не заполнилось. Иначе разметка знает будущее.
        """
        high, low, close = series()
        atr = core.atr_series(high, low, close)
        plist = core.pivots(high, low, bars=3)
        at = 120
        pools = core.find_pools(plist, at, float(atr[at]), 'H')
        for pool in pools:
            assert pool['last'] <= at - 3

    def test_pools_do_not_change_when_future_is_added(self):
        high, low, close = series(300)
        atr = core.atr_series(high, low, close)
        cut = 200
        early = core.find_pools(core.pivots(high[:cut], low[:cut], bars=3),
                                cut - 10, float(atr[cut - 10]), 'H')
        full = core.find_pools(core.pivots(high, low, bars=3),
                               cut - 10, float(atr[cut - 10]), 'H')
        assert [p['price'] for p in early] == [p['price'] for p in full]


class TestPools:
    def test_two_equal_highs_make_a_pool(self):
        """«Равные максимумы» — это ровно про два касания, не про три."""
        plist = [(10, 100.0, 'H', 13), (30, 100.1, 'H', 33)]
        pools = core.find_pools(plist, 50, 1.0, 'H', tolerance=0.5,
                               min_touches=2)
        assert len(pools) == 1
        assert pools[0]['touches'] == 2

    def test_edge_is_the_extreme_not_the_average(self):
        """
        Стопы стоят за КРАЙНЕЙ ценой скопления. Средняя занизила бы и глубину
        выноса, и цель.
        """
        plist = [(10, 100.0, 'H', 13), (30, 100.4, 'H', 33)]
        pools = core.find_pools(plist, 50, 1.0, 'H', tolerance=0.5,
                               min_touches=2)
        assert pools[0]['price'] == pytest.approx(100.4)

    def test_far_apart_pivots_are_not_one_pool(self):
        plist = [(10, 100.0, 'H', 13), (30, 108.0, 'H', 33)]
        assert core.find_pools(plist, 50, 1.0, 'H', tolerance=0.5,
                               min_touches=2) == []

    def test_stale_pools_are_dropped(self):
        """Стопы за полугодовым максимумом давно сняты или отменены."""
        plist = [(10, 100.0, 'H', 13), (12, 100.1, 'H', 15)]
        assert core.find_pools(plist, 500, 1.0, 'H', tolerance=0.5,
                               min_touches=2, max_age=100) == []


def scene():
    """
    Искусственный график: скопление максимумов на 110, минимумов на 90,
    вынос вверх за 110 и возврат внутрь на последнем баре.

    ФОН НАМЕРЕННО НЕРОВНЫЙ. Первая версия держала все максимумы на 105, а
    минимумы на 95 — и каждый такой бар оказывался пивотом, потому что был
    равен минимуму своего окна. Ровный фон породил скопление на 95, код честно
    выбрал его как ближайшее впереди, и тест «упал» на верном поведении.
    Настоящий график ровным не бывает, а проверять надо код, а не артефакт
    сцены.
    """
    n = 60
    rng = np.random.default_rng(1)
    drift = rng.normal(0, 0.4, n)
    high = 105.0 + drift
    low = 95.0 + drift
    close = 100.0 + drift
    # Равные максимумы: два пивота на 110.
    for i in (10, 25):
        high[i] = 110.0
    # Равные минимумы: два пивота на 90.
    for i in (15, 30):
        low[i] = 90.0
    # Вынос вверх за 110 и закрытие обратно.
    high[55] = 113.0
    close[55] = 108.0
    high[56] = 109.0
    close[56] = 106.0
    atr = np.full(n, 2.0)
    return high, low, close, atr


class TestSweep:
    def test_pierce_and_reclaim_found(self):
        high, low, close, atr = scene()
        plist = core.pivots(high, low, bars=3)
        setup = core.find_sweep(high, low, close, 56, plist, atr,
                                pierce_atr=0.2, reclaim_bars=4,
                                tolerance=0.02, min_touches=2, max_age=300)
        assert setup is not None
        assert setup['direction'] == 'SHORT'
        assert setup['pool']['price'] == pytest.approx(110.0)
        assert setup['extreme'] == pytest.approx(113.0)
        assert setup['target_pool']['price'] == pytest.approx(90.0)

    def test_shallow_touch_is_not_a_sweep(self):
        high, low, close, atr = scene()
        high[55] = 110.1                       # едва задели
        plist = core.pivots(high, low, bars=3)
        assert core.find_sweep(high, low, close, 56, plist, atr,
                               pierce_atr=0.2, reclaim_bars=4,
                               tolerance=0.02, min_touches=2) is None

    def test_no_reclaim_is_not_a_sweep(self):
        """Цена осталась ЗА уровнем — это выход, а не вынос."""
        high, low, close, atr = scene()
        close[56] = 112.0
        plist = core.pivots(high, low, bars=3)
        assert core.find_sweep(high, low, close, 56, plist, atr,
                               pierce_atr=0.2, reclaim_bars=4,
                               tolerance=0.02, min_touches=2) is None


class TestTargetBeyondTheFence:
    def test_target_sits_beyond_the_opposite_pool(self):
        """
        В этом весь сетап. Цель НА уровне превращает его в обычный отбой, а
        короткая цель уже дважды губила замеры.
        """
        high, low, close, atr = scene()
        plist = core.pivots(high, low, bars=3)
        setup = core.find_sweep(high, low, close, 56, plist, atr,
                                pierce_atr=0.2, reclaim_bars=4,
                                tolerance=0.02, min_touches=2)
        trade = core.build_trade(setup, stop_pad_atr=0.25, beyond_atr=0.5,
                                 min_rr=0.0, min_stop_pct=0.0)
        # Скопление минимумов на 90, ATR 2.0, запас 0.5 ATR = 1.0 → цель 89.
        assert trade['target'] == pytest.approx(89.0)
        assert trade['target'] < setup['target_pool']['price']
        assert trade['stop'] == pytest.approx(113.5)   # экстремум + 0.25 ATR

    def test_zero_beyond_puts_target_on_the_pool(self):
        high, low, close, atr = scene()
        plist = core.pivots(high, low, bars=3)
        setup = core.find_sweep(high, low, close, 56, plist, atr,
                                pierce_atr=0.2, reclaim_bars=4,
                                tolerance=0.02, min_touches=2)
        trade = core.build_trade(setup, beyond_atr=0.0, min_rr=0.0,
                                 min_stop_pct=0.0)
        assert trade['target'] == pytest.approx(90.0)

    def test_min_rr_rejects_bad_geometry(self):
        high, low, close, atr = scene()
        plist = core.pivots(high, low, bars=3)
        setup = core.find_sweep(high, low, close, 56, plist, atr,
                                pierce_atr=0.2, reclaim_bars=4,
                                tolerance=0.02, min_touches=2)
        assert core.build_trade(setup, stop_pad_atr=20.0, min_rr=1.5,
                                min_stop_pct=0.0) is None
