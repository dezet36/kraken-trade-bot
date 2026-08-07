"""
Волновая разметка не должна читать будущее.

ЗАЧЕМ ОТДЕЛЬНЫЙ ТЕСТ. Зигзаг — единственный инструмент в проекте, где пивот
лежит В ПРОШЛОМ относительно момента, когда он становится известен. Разметка,
нарисованная по готовому графику, всегда выглядит безупречно, и ошибка здесь не
падает с исключением, а тихо выдаёт прибыльный замер, который невозможно
повторить в бою.

Проверка прямая: разметка первых N баров не должна зависеть от того, что идёт
после них.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wave import core  # noqa: E402


def series(n=600, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    return high, low, close


def test_pivots_do_not_change_when_future_is_added():
    high, low, close = series()
    cut = 400
    early = core.zigzag(high[:cut], low[:cut], close[:cut])
    full = core.zigzag(high, low, close)

    # Пивоты, подтверждённые до среза, обязаны совпасть до последнего поля.
    settled = [p for p in full if p['confirmed_at'] < cut]
    assert settled, 'разметка пуста — тест ничего не проверяет'
    assert early[:len(settled)] == settled


def test_pivot_is_confirmed_after_itself():
    high, low, close = series()
    pivots = core.zigzag(high, low, close)
    assert pivots
    for p in pivots:
        assert p['confirmed_at'] >= p['index']


def test_pivots_alternate_sides():
    high, low, close = series()
    pivots = core.zigzag(high, low, close)
    kinds = [p['kind'] for p in pivots]
    assert all(a != b for a, b in zip(kinds, kinds[1:]))


def test_decision_bar_is_the_confirmation_bar():
    high, low, close = series()
    atr = core.atr_series(high, low, close)
    pivots = core.zigzag(high, low, close, atr=atr)
    found = 0
    for k in range(len(pivots)):
        for mode in ('limit', 'market'):
            wave = core.find_wave(pivots, k, atr, entry_mode=mode)
            if wave is None:
                continue
            found += 1
            last = wave['c'] or wave['b']
            assert wave['at'] == last['confirmed_at']
            for point in ('a', 'b'):
                assert wave[point]['confirmed_at'] <= wave['at']
    assert found, 'ни одной волны — тест ничего не проверяет'


def test_rule_one_is_enforced():
    """Откат за начало волны 1 отменяет разметку — это и есть правило 1."""
    atr = np.full(50, 1.0)
    pivots = [
        {'index': 0, 'price': 100.0, 'kind': 'L', 'confirmed_at': 1},
        {'index': 10, 'price': 120.0, 'kind': 'H', 'confirmed_at': 12},
        {'index': 20, 'price': 99.0, 'kind': 'L', 'confirmed_at': 22},
    ]
    assert core.find_wave(pivots, 2, atr, entry_mode='market') is None

    pivots[2]['price'] = 110.0            # откат на 50% — разметка в силе
    wave = core.find_wave(pivots, 2, atr, entry_mode='market')
    assert wave is not None
    assert wave['retrace'] == pytest.approx(0.5)


def test_limit_entry_rejected_when_level_already_passed():
    """
    Заявка не может встать на уровень, который цена уже прошла.

    Без этой проверки движок нальёт лимит по его цене — то есть лучше рынка, и
    замер получит бесплатный обед на каждой второй сделке.
    """
    atr = np.full(50, 1.0)
    pivots = [
        {'index': 0, 'price': 100.0, 'kind': 'L', 'confirmed_at': 1},
        {'index': 10, 'price': 120.0, 'kind': 'H', 'confirmed_at': 12},
    ]
    wave = core.find_wave(pivots, 1, atr, entry_mode='limit')
    assert wave is not None
    # Уровень 50% — это 110. Цена уже на 105, ниже уровня.
    assert core.build_trade(wave, price_now=105.0, entry_retrace=0.5) is None
    trade = core.build_trade(wave, price_now=115.0, entry_retrace=0.5)
    assert trade is not None
    assert trade['entry'] == pytest.approx(110.0)


def test_stop_sits_beyond_wave_one_start():
    atr = np.full(50, 1.0)
    pivots = [
        {'index': 0, 'price': 100.0, 'kind': 'L', 'confirmed_at': 1},
        {'index': 10, 'price': 120.0, 'kind': 'H', 'confirmed_at': 12},
    ]
    wave = core.find_wave(pivots, 1, atr, entry_mode='limit')
    trade = core.build_trade(wave, price_now=115.0, entry_retrace=0.5,
                             stop_pad_atr=0.25, target_ext=1.618)
    assert trade['stop'] == pytest.approx(99.75)      # 100 − 0.25 × ATR
    assert trade['target'] == pytest.approx(132.36)   # 100 + 1.618 × 20
    assert trade['rr'] == pytest.approx(22.36 / 10.25, rel=1e-3)


def test_short_side_is_symmetric():
    atr = np.full(50, 1.0)
    pivots = [
        {'index': 0, 'price': 120.0, 'kind': 'H', 'confirmed_at': 1},
        {'index': 10, 'price': 100.0, 'kind': 'L', 'confirmed_at': 12},
    ]
    wave = core.find_wave(pivots, 1, atr, entry_mode='limit')
    assert wave['direction'] == 'SHORT'
    trade = core.build_trade(wave, price_now=105.0, entry_retrace=0.5,
                             stop_pad_atr=0.25, target_ext=1.618)
    assert trade['entry'] == pytest.approx(110.0)
    assert trade['stop'] == pytest.approx(120.25)
    assert trade['target'] == pytest.approx(87.64)
