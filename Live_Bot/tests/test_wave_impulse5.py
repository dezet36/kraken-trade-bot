"""
Полная разметка 1-2-3-4-5: три строгих правила и скоринг Фибоначчи.

ЗАЧЕМ ТЕСТЫ ИМЕННО ЗДЕСЬ. Правила формулируются словами «волна 4 не заходит на
территорию волны 1», а в коде превращаются в сравнение двух цен со знаком,
который зависит от направления импульса. Перепутать знак — значит отвергать
годные разметки и принимать нарушенные, причём молча: исключения не будет,
сетапы просто станут другими.

Каждое правило проверяется в обе стороны: что нарушение отвергается И что
допустимый случай проходит. Тест только на отказ пропустил бы реализацию,
которая отвергает вообще всё.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wave import impulse as imp  # noqa: E402

ATR = np.full(200, 1.0)


def pivots(prices, up=True, step=10):
    """Шесть пивотов по заданным ценам, чередующихся по стороне."""
    kinds = ['L', 'H'] * 3 if up else ['H', 'L'] * 3
    return [{'index': i * step, 'price': float(price), 'kind': kinds[i],
             'confirmed_at': i * step + 3}
            for i, price in enumerate(prices)]


# Эталонный восходящий импульс: 100 → 120 → 110 → 150 → 140 → 165.
# w1=20, w2=10 (50%), w3=40 (200% w1), w4=10 (25% w3), w5=25.
GOOD = [100, 120, 110, 150, 140, 165]


class TestValidImpulsePasses:
    def test_textbook_impulse_is_accepted(self):
        wave = imp.find_impulse(pivots(GOOD), 5, ATR, min_wave_atr=1.0)
        assert wave is not None
        assert wave['direction'] == 'LONG'
        assert wave['waves']['w1'] == pytest.approx(20)
        assert wave['waves']['w3'] == pytest.approx(40)
        assert wave['ratios']['w2_of_w1'] == pytest.approx(0.5)

    def test_mirror_impulse_is_accepted(self):
        down = [165, 140, 150, 110, 120, 100]
        wave = imp.find_impulse(pivots(down, up=False), 5, ATR, min_wave_atr=1.0)
        assert wave is not None
        assert wave['direction'] == 'SHORT'


class TestRuleOne:
    """Волна 2 не откатывает больше 100% волны 1."""

    def test_deep_retrace_rejected(self):
        broken = [100, 120, 99, 150, 140, 165]      # волна 2 ушла ниже старта
        assert imp.find_impulse(pivots(broken), 5, ATR, min_wave_atr=1.0) is None

    def test_retrace_just_inside_accepted(self):
        edge = [100, 120, 100.5, 150, 140, 165]
        assert imp.find_impulse(pivots(edge), 5, ATR, min_wave_atr=1.0)


class TestRuleTwo:
    """Волна 3 не самая короткая из 1, 3 и 5."""

    def test_shortest_third_rejected(self):
        # w1=20, w3=5, w5=30 — третья короче обеих.
        broken = [100, 120, 110, 115, 112, 142]
        assert imp.find_impulse(pivots(broken), 5, ATR, min_wave_atr=1.0) is None

    def test_third_shorter_than_one_only_accepted(self):
        """Правило запрещает быть короче ОБЕИХ, а не одной."""
        # w1=30, w3=20, w5=10 — третья короче первой, но длиннее пятой.
        ok = [100, 130, 120, 140, 135, 145]
        assert imp.find_impulse(pivots(ok), 5, ATR, min_wave_atr=1.0)


class TestRuleThree:
    """Волна 4 не заходит в ценовую область волны 1."""

    def test_overlap_rejected(self):
        # Волна 4 опустилась ниже вершины волны 1 (120).
        broken = [100, 120, 110, 150, 118, 165]
        assert imp.find_impulse(pivots(broken), 5, ATR, min_wave_atr=1.0) is None

    def test_no_overlap_accepted(self):
        ok = [100, 120, 110, 150, 121, 165]
        assert imp.find_impulse(pivots(ok), 5, ATR, min_wave_atr=1.0)

    def test_rule_three_mirrored_for_shorts(self):
        broken = [165, 140, 150, 110, 142, 100]     # волна 4 выше вершины 1
        assert imp.find_impulse(pivots(broken, up=False), 5, ATR,
                                min_wave_atr=1.0) is None


class TestTruncation:
    def test_truncated_fifth_rejected_by_default(self):
        # Волна 5 не превысила вершину третьей (150).
        cut = [100, 120, 110, 150, 140, 148]
        assert imp.find_impulse(pivots(cut), 5, ATR, min_wave_atr=1.0) is None

    def test_truncated_allowed_when_asked(self):
        cut = [100, 120, 110, 150, 140, 148]
        wave = imp.find_impulse(pivots(cut), 5, ATR, min_wave_atr=1.0,
                                allow_truncation=True)
        assert wave and wave['truncated'] is True


class TestFibonacciScore:
    def test_textbook_ratios_score_higher_than_random(self):
        """
        Скоринг обязан отличать каноническую разметку от произвольной, иначе
        он не фильтр, а украшение.
        """
        good = imp.fib_score({'w2_of_w1': 0.618, 'w3_of_w1': 1.618,
                              'w4_of_w3': 0.382, 'w5_of_w1': 1.0})
        poor = imp.fib_score({'w2_of_w1': 0.15, 'w3_of_w1': 1.02,
                              'w4_of_w3': 0.93, 'w5_of_w1': 0.21})
        assert good > 0.9
        assert poor < 0.2
        assert good > poor

    def test_tolerance_is_relative_not_absolute(self):
        """
        Промах на 0.05 при цели 0.5 и при цели 2.618 — разная точность.
        Абсолютный допуск требовал бы от расширений недостижимого совпадения.
        """
        near_small = imp._closeness(0.55, (0.5,))
        near_large = imp._closeness(2.88, (2.618,))
        assert near_small == pytest.approx(near_large, abs=0.05)


class TestEntryNeedsCurrentPrice:
    def test_entry_uses_price_now_not_the_pivot(self):
        """
        Конец волны 4 известен только ПОСЛЕ разворота от него: войти по его
        цене нельзя, это была бы та же утечка, что и в прошлой реализации.
        """
        wave = imp.find_impulse(pivots(GOOD), 5, ATR, min_wave_atr=1.0)
        trade = imp.wave_four_entry(wave, price_now=143.0, min_rr=0.0,
                                    min_stop_pct=0.0, stop_pad_atr=0.25)
        assert trade['entry'] == pytest.approx(143.0)
        assert trade['stop'] == pytest.approx(139.75)     # конец волны 4 − 0.25
        # Правило равенства: волна 5 повторяет волну 1 (20) от конца волны 4.
        assert trade['target'] == pytest.approx(160.0)

    def test_stop_below_entry_for_longs(self):
        wave = imp.find_impulse(pivots(GOOD), 5, ATR, min_wave_atr=1.0)
        # Цена УЖЕ ниже конца волны 4 — вход невозможен, стоп оказался бы выше.
        assert imp.wave_four_entry(wave, price_now=139.0, min_rr=0.0,
                                   min_stop_pct=0.0) is None


class TestNoLookahead:
    def test_decision_bar_is_the_last_confirmation(self):
        wave = imp.find_impulse(pivots(GOOD), 5, ATR, min_wave_atr=1.0)
        assert wave['at'] == pivots(GOOD)[5]['confirmed_at']
        for point in wave['points']:
            assert point['confirmed_at'] <= wave['at']
