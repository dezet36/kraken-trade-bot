"""
Фибо 12ч: боевой расчёт обязан повторять исследовательский движок.

ЗАЧЕМ. Замер (research/fibz) считал свинги, ноги и геометрию на numba по всей
истории, бот — на чистом Python по окну последних HISTORY_BARS закрытых баров.
Эталон записан исследовательским движком на реальных свечах Bybit 12ч
(research/fibz/make_live_fixture.py → tests/data/fib12_golden.json), и здесь
требуется совпадение: торговаться должно ровно то, что измерено.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fib12 import core, params  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'fib12_golden.json')


@pytest.fixture(scope='module')
def golden():
    with open(GOLDEN, encoding='utf-8') as fh:
        return json.load(fh)


def close(a, b, rel=1e-9):
    return abs(a - b) <= rel * max(1.0, abs(a), abs(b))


def window(g, i, n=None):
    n = n or params.HISTORY_BARS
    s = slice(max(0, i + 1 - n), i + 1)
    return g['o'][s], g['h'][s], g['l'][s], g['c'][s]


class TestMatchesResearchEngine:
    def test_legs_identical_on_bot_window(self, golden):
        """Ноги, подтверждённые на последнем баре окна бота, — те же, что у
        замера по полной истории: те же A, B, стороны."""
        checked = 0
        for pair, g in golden.items():
            by_conf = {}
            for leg in g['legs']:
                by_conf.setdefault(leg['conf'], []).append(leg)
            for i, want in by_conf.items():
                o, h, l, c = window(g, i)
                got = [x for x in core.legs(h, l, params.SWING_K) if x['conf'] == len(c) - 1]
                assert len(got) == len(want), (pair, i)
                for x, w in zip(got, want):
                    assert x['dir'] == w['dir'], (pair, i)
                    assert close(x['A'], w['A']) and close(x['B'], w['B']), (pair, i)
                    checked += 1
        assert checked > 200

    def test_setup_geometry_identical(self, golden):
        """Вход, стоп и цель — как в замере. ATR окна (500 баров) и полной
        истории сходятся до 1e-6: память сглаживания Уайлдера ~14 баров."""
        checked = 0
        for pair, g in golden.items():
            by_conf = {}
            for leg in g['legs']:
                by_conf.setdefault(leg['conf'], []).append(leg)
            for i, legs in by_conf.items():
                o, h, l, c = window(g, i)
                setup, why = core.evaluate(o, h, l, c, k=params.SWING_K, atr_period=params.ATR_PERIOD,
                                           retrace=params.RETRACE, stop_level=params.STOP_LEVEL,
                                           stop_buffer_atr=params.STOP_BUFFER_ATR,
                                           target_ext=params.TARGET_EXT)
                valid = [w for w in legs if w['valid']]
                if not valid:
                    assert setup is None, (pair, i, why)
                    continue
                want = valid[0]
                assert setup is not None, (pair, i, why)
                assert setup['dir'] == want['dir']
                assert close(setup['entry'], want['entry']), (pair, i)
                assert close(setup['target'], want['target']), (pair, i)
                assert close(setup['atr'], want['atr'], 1e-6), (pair, i)
                assert close(setup['stop'], want['stop'], 1e-6), (pair, i)
                checked += 1
        assert checked > 80


class TestLive:
    def test_no_lookahead(self, golden):
        """Обрезанный ряд даёт те же ноги, что полный, до точки обреза."""
        g = golden['ETHUSDT']
        full = core.legs(g['h'], g['l'], 3)
        for cut in (300, 611, 820):
            part = core.legs(g['h'][:cut], g['l'][:cut], 3)
            assert part == [x for x in full if x['conf'] < cut]

    def test_bar_without_leg_gives_nothing(self, golden):
        g = golden['SOLUSDT']
        confs = {x['conf'] for x in core.legs(g['h'], g['l'], 3)}
        i = next(j for j in range(600, len(g['c'])) if j not in confs)
        setup, why = core.evaluate(*window(g, i))
        assert setup is None and 'ноги нет' in why

    def test_geometry_of_a_long_and_a_short(self, golden):
        """Вход на 0.382 от B, стоп за 0.786 ноги, цель за B на 1.618 ноги."""
        g = golden['COTIUSDT']
        seen = set()
        for w in g['legs']:
            if not w['valid'] or w['dir'] in seen:
                continue
            setup, _ = core.evaluate(*window(g, w['conf']))
            L = setup['size']
            d = setup['dir']
            assert close(setup['entry'], setup['B'] - d * 0.382 * L)
            assert (setup['stop'] - (setup['B'] - d * 0.786 * L)) * d < 0     # глубже 0.786
            assert close(setup['target'], setup['B'] + d * 1.618 * L)
            assert (setup['ref'] - setup['entry']) * d > 0                      # откат ещё не дошёл
            seen.add(d)
        assert seen == {1, -1}
