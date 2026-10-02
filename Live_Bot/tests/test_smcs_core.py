"""
SMC-структура 4ч: боевой расчёт обязан повторять исследовательский движок.

ЗАЧЕМ. Замер (research/smcz) считал свинги, сломы и стоп на numba, бот — на
чистом Python. Два расчёта одной идеи расходятся незаметно: строгое «>» против
«>=» у фрактала сдвигает свинг, свинг — слом, слом — сделку. Эталон записан
исследовательским движком на реальных свечах Bybit 4ч
(research/smcz/make_live_fixture.py → tests/data/smcs_golden.json), и здесь
требуется точное совпадение: торговаться должно ровно то, что измерено.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smcs import core  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'smcs_golden.json')


@pytest.fixture(scope='module')
def golden():
    with open(GOLDEN, encoding='utf-8') as fh:
        return json.load(fh)


def close(a, b):
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


class TestMatchesResearchEngine:
    def test_events_identical(self, golden):
        for pair, g in golden.items():
            _, events = core.market_structure(g['o'], g['h'], g['l'], g['c'], 3)
            assert len(events) == len(g['events']), pair
            for got, want in zip(events, g['events']):
                assert got['bar'] == want['bar'], pair
                assert got['dir'] == want['dir'], (pair, want['bar'])
                assert got['kind'] == want['kind'], (pair, want['bar'])
                for key in ('level', 'org_px', 'ext', 'ob_hi', 'ob_lo'):
                    assert close(got[key], want[key]), (pair, want['bar'], key)

    def test_setup_geometry_identical(self, golden):
        """Стоп и цель на баре слома — как в замере (обрезанный ряд: бар
        слома последний, так видит его бот)."""
        checked = 0
        for pair, g in golden.items():
            for want in g['events']:
                i = want['bar']
                if i < 60:
                    continue
                cut = slice(0, i + 1)
                setup, why = core.evaluate(g['o'][cut], g['h'][cut], g['l'][cut], g['c'][cut],
                                           k=3, atr_period=14, stop_buffer_atr=0.1,
                                           target_r=8.0, bos_only=False)
                assert setup is not None, (pair, i, why)
                assert setup['dir'] == want['dir']
                assert close(setup['atr'], want['atr']), (pair, i)
                assert close(setup['stop'], want['stop']), (pair, i)
                assert close(setup['target'], want['target']), (pair, i)
                checked += 1
        assert checked > 60


class TestLive:
    def test_no_lookahead(self, golden):
        """Обрезанный ряд даёт те же события, что полный, до точки обреза."""
        g = golden['ETHUSDT']
        _, full = core.market_structure(g['o'], g['h'], g['l'], g['c'], 3)
        for cut in (200, 377, 512):
            _, part = core.market_structure(g['o'][:cut], g['h'][:cut], g['l'][:cut],
                                            g['c'][:cut], 3)
            assert part == [e for e in full if e['bar'] < cut]

    def test_choch_refused_when_bos_only(self, golden):
        g = golden['COTIUSDT']
        choch = [e for e in g['events'] if e['kind'] == 'CHoCH' and e['bar'] > 60]
        assert choch
        i = choch[0]['bar']
        setup, why = core.evaluate(g['o'][:i + 1], g['h'][:i + 1], g['l'][:i + 1],
                                   g['c'][:i + 1], bos_only=True)
        assert setup is None and 'CHoCH' in why

    def test_bar_without_break_gives_nothing(self, golden):
        g = golden['SHIB1000USDT']
        bars = {e['bar'] for e in g['events']}
        i = next(j for j in range(100, len(g['c'])) if j not in bars)
        setup, why = core.evaluate(g['o'][:i + 1], g['h'][:i + 1], g['l'][:i + 1],
                                   g['c'][:i + 1])
        assert setup is None and 'слома нет' in why

    def test_stop_beyond_leg_start_target_eight_r(self, golden):
        g = golden['ETHUSDT']
        bos = [e for e in g['events'] if e['kind'] == 'BOS' and e['bar'] > 60]
        e = bos[0]
        i = e['bar']
        setup, _ = core.evaluate(g['o'][:i + 1], g['h'][:i + 1], g['l'][:i + 1], g['c'][:i + 1])
        ref = g['c'][i]
        if setup['dir'] == 1:
            assert setup['stop'] < setup['ob_lo'] <= setup['org_px'] < ref
        else:
            assert setup['stop'] > setup['ob_hi'] >= setup['org_px'] > ref
        assert close(abs(setup['target'] - ref), 8.0 * abs(ref - setup['stop']))
