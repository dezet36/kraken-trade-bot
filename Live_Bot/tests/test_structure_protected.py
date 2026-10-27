"""
Классическая структура (analysis/smc/params.STRUCTURE_MODE='protected', аудит
27.10.2026): смена тренда — только пробой защищённого свинга; пробой мелкого
отката внутри тренда — не событие. По умолчанию ('fractal') всё как было.
"""
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis.smc import params, structure  # noqa: E402

#            0    1    2    3    4    5:H1   6    7    8    9:L1  10   11   12   13   14:BOS 15   16:H2 17   18
CLOSES = [100, 101, 102, 103, 104, 105, 104, 103, 102, 101, 102, 103, 104, 105, 106, 107, 108, 107, 106,
          105.5, 106, 106.5, 105.2, 104, 102, 100.5, 100.3, 100.2]
#         19:L2 20   21    22: ниже L2, выше L1      25: ниже защищённого L1


def frame():
    c = pd.Series(CLOSES, dtype=float)
    return pd.DataFrame({'timestamp': pd.date_range('2026-01-01', periods=len(c), freq='h'),
                         'open': c, 'high': c + 0.1, 'low': c - 0.1, 'close': c})


def events(mode, monkeypatch):
    monkeypatch.setattr(params, 'STRUCTURE_MODE', mode)
    return [(e['index'], e['type'], e['direction']) for e in structure.build_structure(frame(), n=2)['events']]


def test_default_is_unchanged():
    assert params.STRUCTURE_MODE == 'fractal'


def test_fractal_turns_on_a_minor_pullback(monkeypatch):
    ev = events('fractal', monkeypatch)
    assert ev[0][0] == 14 and ev[0][2] == 'BULLISH', ev       # пробой H1 — первый слом вверх
    assert (22, 'CHOCH', 'BEARISH') in ev, ev                  # пробой мелкого отката L2 — уже CHoCH


def test_protected_ignores_the_minor_pullback_and_turns_on_the_protected_low(monkeypatch):
    ev = events('protected', monkeypatch)
    assert all(i != 22 for i, _, _ in ev), ev
    assert (25, 'CHOCH', 'BEARISH') in ev, ev


def test_minor_tier_does_not_depend_on_the_mode(monkeypatch):
    monkeypatch.setattr(params, 'STRUCTURE_MODE', 'protected')
    a = structure.build_structure(frame(), tier='minor')['events']
    monkeypatch.setattr(params, 'STRUCTURE_MODE', 'fractal')
    assert structure.build_structure(frame(), tier='minor')['events'] == a


@pytest.mark.parametrize('mode', ['fractal', 'protected'])
def test_no_look_ahead(mode, monkeypatch):
    """Сломы на свечах до k не меняются, если дописать свечи после k."""
    monkeypatch.setattr(params, 'STRUCTURE_MODE', mode)
    full = structure.build_structure(frame(), n=2)['events']
    for k in range(10, len(CLOSES)):
        cut = structure.build_structure(frame().iloc[:k + 1], n=2)['events']
        assert [e for e in full if e['index'] <= k] == cut
