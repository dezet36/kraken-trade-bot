"""
Тесты целей на пулах ликвидности (TP_MODE='liquidity').

Зачем режим нужен. Разбор 471 сделки показал, что сделка в среднем проходит
2.7R в нашу сторону, но 68-70% не фиксируют НИ ОДНОЙ цели. Цели по сетке
Фибоначчи отсчитываются за конец импульса и часто оказываются там, где
ликвидности нет вовсе — цене незачем туда идти. Методичка §14.2 предписывает
обратное: тейки на очевидных пулах ликвидности.

Проверяется то, что ломается молча: снятые уровни в целях, дубликаты уровней,
цели ближе комиссии, потеря сетапа при отсутствии пулов.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc import liquidity, params, signal   # noqa: E402

BULLISH = 'BULLISH'
BEARISH = 'BEARISH'


def pool(price, source='SWING', weight=0.5, side=liquidity.BSL, confirmed=0):
    return {'side': side, 'price': price, 'index': confirmed,
            'confirmed_at': confirmed, 'source': source, 'weight': weight}


class Ctx:
    """Минимальный контекст: методу нужны только пулы и свипы."""

    def __init__(self, pools, sweeps=()):
        self.pools = list(pools)
        self.sweeps = list(sweeps)

    targets = signal.MarketContext._liquidity_targets


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(params, 'LIQ_MIN_R', 1.0)
    monkeypatch.setattr(params, 'LIQ_MERGE_PCT', 0.0015)
    monkeypatch.setattr(params, 'LIQ_MIN_WEIGHT', 0.0)


class TestSelection:
    def test_nearest_pools_become_targets_in_order(self):
        ctx = Ctx([pool(112), pool(130), pool(121)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[999.0])

        assert out == [112.0, 121.0, 130.0]

    def test_pools_behind_entry_ignored(self):
        """Цель за спиной — не цель: цена туда уже сходила."""
        ctx = Ctx([pool(90), pool(95), pool(115)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[])

        assert out == [115.0]

    def test_short_takes_pools_below(self):
        ctx = Ctx([pool(88, side=liquidity.SSL), pool(75, side=liquidity.SSL),
                   pool(120, side=liquidity.BSL)])
        out = ctx.targets(BEARISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[])

        assert out == [88.0, 75.0]

    def test_too_close_target_rejected(self):
        """Цель в 0.5R не окупает комиссию и проскальзывание."""
        ctx = Ctx([pool(105), pool(118)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[])

        assert out == [118.0]

    def test_weight_filter_drops_minor_levels(self):
        """При включённом пороге значимости мелкие свинги не берутся."""
        params.LIQ_MIN_WEIGHT = 0.8
        try:
            ctx = Ctx([pool(115, 'SWING', 0.5), pool(140, 'PWH', 0.85)])
            out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                              count=3, fallback=[])
        finally:
            params.LIQ_MIN_WEIGHT = 0.0

        assert out == [140.0]


class TestSweptPools:
    def test_swept_level_is_not_a_target(self):
        """
        Снятый уровень ликвидности больше не притягивает цену: стопы за ним
        уже собраны. Цель там означала бы ожидание движения без причины.
        """
        target_pool = pool(115, 'EQH', 0.9)
        ctx = Ctx([target_pool, pool(130)],
                  sweeps=[{'index': 10, 'level': 115.0, 'pool': target_pool}])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[])

        assert out == [130.0]

    def test_pool_confirmed_later_is_invisible(self):
        """Уровень, ставший известным ПОЗЖЕ входа, — подглядывание в будущее."""
        ctx = Ctx([pool(115, confirmed=80), pool(130, confirmed=10)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[])

        assert out == [130.0]


class TestMerging:
    def test_duplicate_levels_collapse(self):
        """
        Свинговый хай и хай прошлого дня часто стоят в одной точке. Две цели
        по одной цене раздробили бы позицию без всякого смысла.
        """
        ctx = Ctx([pool(115.00, 'SWING', 0.5), pool(115.05, 'PDH', 0.7),
                   pool(140.0, 'PWH', 0.85)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[])

        assert out == [115.05, 140.0]      # остался более значимый из пары

    def test_distinct_levels_survive(self):
        ctx = Ctx([pool(115.0), pool(118.0)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[])

        assert out == [115.0, 118.0]


class TestFallback:
    def test_no_pools_ahead_falls_back_to_grid(self):
        """
        Без запасного варианта сетап терялся бы целиком там, где он может быть
        хорош, — а пулов впереди не бывает при выходе на исторический максимум.
        """
        ctx = Ctx([pool(90), pool(95)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[130.0, 160.0])

        assert out == [130.0, 160.0]

    def test_missing_targets_topped_up_from_grid(self):
        ctx = Ctx([pool(115)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[110.0, 130.0, 160.0])

        # 110 лежит БЛИЖЕ найденного пула и не годится: цели обязаны идти по
        # возрастанию удалённости, иначе доли фиксации применятся не к тем уровням
        assert out == [115.0, 130.0, 160.0]

    def test_targets_never_exceed_fraction_count(self):
        ctx = Ctx([pool(112), pool(121), pool(130), pool(145)])
        out = ctx.targets(BULLISH, entry=100.0, sl_distance=10.0, at_index=50,
                          count=3, fallback=[])

        assert len(out) == 3
