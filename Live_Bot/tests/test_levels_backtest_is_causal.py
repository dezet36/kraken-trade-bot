"""
Бэктест уровней не знает будущего: свечи, добавленные ПОСЛЕ решения, не
меняют решения.

ОТКУДА (26.09.2026). research/levels_backtest строил уровни один раз по всей
истории. Касание, случившееся позже, склеивалось с прошлыми, и уровень,
которому ещё предстояло его получить, прятался до того момента: решение на
свече i зависело от свечей после i. Бот так не может — он строит уровни по
последним закрытым свечам. На трёх периодах прежний бэктест давал +0.35…+0.54R
на сделку, бой на тех же свечах — от −0.42 до +0.38R (levels_live_sim.py).

Проверка — на свойстве, а не на числах: допишите в конец свечи, где уровень
касаются ещё раз, — прежнее решение на прежней свече измениться не должно.
Прежний способ (causal=False) эту проверку ПРОВАЛИВАЕТ — на том и держится
уверенность, что пример вообще способен поймать утечку.
"""

import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'research'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

lb = pytest.importorskip(
    'levels_backtest', reason='бэктест (research/) отсутствует — это серверная сборка')

from test_levels_live_matches_backtest import SUPPORT, market  # noqa: E402


def with_future_touch(df, extra=14, at=6):
    """Свечи ПОСЛЕ решения: цена отходит и ещё раз касается поддержки."""
    rows, t = [], df['timestamp'].iloc[-1]
    base = SUPPORT + 2.0
    for k in range(1, extra + 1):
        rows.append({'timestamp': t + pd.Timedelta(hours=k), 'open': base, 'high': base + 0.3,
                     'low': SUPPORT if k == at else base - 0.3, 'close': base, 'volume': 100.0})
    return pd.concat([df, pd.DataFrame(rows)], ignore_index=True)


def arrays(df):
    return tuple(df[c].to_numpy(float) for c in ('high', 'low', 'close', 'volume'))


def test_the_old_way_is_changed_by_the_future_and_the_new_is_not():
    past = market(bars=180)
    future = with_future_touch(past)
    i = len(past) - 1

    old_past, _ = lb.decide(*arrays(past), i, causal=False)
    old_future, _ = lb.decide(*arrays(future), i, causal=False)
    assert old_past is not None, 'на ряду нет сетапа — проверять нечего'
    assert old_future is None, 'прежний способ не меняется от будущего — пример не ловит утечку'

    new_past, _ = lb.decide(*arrays(past), i)
    new_future, _ = lb.decide(*arrays(future), i)
    assert new_past is not None and new_future is not None
    for key in ('direction', 'level', 'entry', 'stop_loss', 'target', 'pierce_index'):
        assert new_future[key] == pytest.approx(new_past[key]), key


def test_orders_before_a_moment_do_not_depend_on_what_comes_after():
    past = market(bars=180)
    future = with_future_touch(past)
    before = [(o.direction, round(o.entry, 8), str(o.created)) for o in lb.build_orders('T', past)]
    after = [(o.direction, round(o.entry, 8), str(o.created)) for o in lb.build_orders('T', future)]
    assert before, 'заявок нет — проверка пустая'
    assert after[:len(before)] == before


def test_the_window_is_the_one_the_bot_sees():
    from levels import params
    assert lb.live_window() == params.LOOKBACK + 4
