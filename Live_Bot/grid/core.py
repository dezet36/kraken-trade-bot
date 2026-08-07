"""
Сетка в коридоре: поиск диапазона и построение уровней.

ОДНА реализация на замер и на бой — правило проекта, нарушенное однажды у
стратегии уровней и стоившее месяца недостоверных наблюдений.

ЧТО ИЩЕТСЯ
    1. Коридор: размах последних WINDOW баров укладывается в MAX_WIDTH_ATR,
       и цена пересекла его середину не меньше MIN_CROSSES раз.
    2. Лестница лимитных заявок внутри: покупки ниже середины, продажи выше.
    3. Общий стоп за границей коридора.

ПОЧЕМУ ДВА УСЛОВИЯ, А НЕ ОДНО. Ширина без числа пересечений — это не боковик,
а любое окно, в которое цена влезла: пологий тренд укладывается в такую рамку
и выглядит коридором. Пересечения середины и отличают одно от другого — в
тренде их одно-два, в боковике шесть-восемь.

ЧЕГО ЗДЕСЬ НЕТ. Заглядывания вперёд: коридор строится по барам строго до
сигнального включительно.
"""

import numpy as np

from . import params

LONG, SHORT = 'LONG', 'SHORT'


def atr(high, low, close, at, period=14):
    """Средний истинный диапазон по барам до `at` включительно."""
    start = max(1, at - period + 1)
    if at < 1:
        return 0.0
    highs, lows = high[start:at + 1], low[start:at + 1]
    prev = close[start - 1:at]
    n = min(len(highs), len(lows), len(prev))
    if n == 0:
        return 0.0
    highs, lows, prev = highs[-n:], lows[-n:], prev[-n:]
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - prev), np.abs(lows - prev)))
    return float(np.mean(tr)) if len(tr) else 0.0


def find_range(high, low, close, at, window=None, max_width=None,
               min_crosses=None):
    """Коридор на баре `at` либо None."""
    window = params.WINDOW if window is None else window
    max_width = params.MAX_WIDTH_ATR if max_width is None else max_width
    min_crosses = params.MIN_CROSSES if min_crosses is None else min_crosses

    if at < window + 20:
        return None
    atr_now = atr(high, low, close, at)
    if atr_now <= 0:
        return None

    seg = slice(at - window + 1, at + 1)
    lo = float(np.min(low[seg]))
    hi = float(np.max(high[seg]))
    width = hi - lo
    if width <= 0 or width > max_width * atr_now:
        return None

    mid = (hi + lo) / 2
    side = np.sign(close[seg] - mid)
    crosses = int(np.sum(side[1:] * side[:-1] < 0))
    if crosses < min_crosses:
        return None

    return {'low': lo, 'high': hi, 'mid': mid, 'width': width,
            'atr': atr_now, 'crosses': crosses,
            'width_pct': width / mid * 100}


def build_levels(box, levels=None, max_filled=None, stop_pad=None,
                 min_stop_pct=None):
    """
    Заявки сетки. Список словарей либо пустой список.

    Цель уровня — СОСЕДНИЙ уровень в сторону прибыли, а не середина и не
    противоположный край. Шаг сетки и есть прибыль сделки: чем он мельче, тем
    чаще берётся и тем большую долю съедают издержки. Где золотая середина —
    решает замер, поэтому число уровней вынесено в параметр.

    Стоп у всех уровней ОБЩИЙ, за границей коридора. Это и делает сетку одной
    ставкой, а не набором независимых: в плохом случае они проигрывают разом.
    Отсюда же и деление риска между уровнями в замере.
    """
    levels = params.LEVELS if levels is None else levels
    max_filled = params.MAX_FILLED if max_filled is None else max_filled
    stop_pad = params.STOP_PAD_ATR if stop_pad is None else stop_pad
    min_stop_pct = params.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct
    if levels < 2:
        return []

    step = box['width'] / levels
    pad = stop_pad * box['atr']
    stop_long = box['low'] - pad
    stop_short = box['high'] + pad

    out = []
    for k in range(1, levels):
        price = box['low'] + step * k
        if price < box['mid']:
            entry, target, stop, side = price, price + step, stop_long, LONG
        elif price > box['mid']:
            entry, target, stop, side = price, price - step, stop_short, SHORT
        else:
            continue                       # ровно середина — сторона неясна

        distance = abs(entry - stop)
        floor = entry * min_stop_pct / 100
        if distance < floor:
            stop = entry - floor if side == LONG else entry + floor
            distance = floor
        if distance <= 0:
            continue

        out.append({'direction': side, 'entry': entry, 'stop': stop,
                    'target': target, 'step': step,
                    'rr': abs(target - entry) / distance,
                    'stop_pct': distance / entry * 100,
                    'level': k})

    # Ближайшие к середине уровни набираются первыми — цена доходит до них
    # раньше. Предел применяется к ним, а не к случайной части лестницы.
    out.sort(key=lambda o: abs(o['entry'] - box['mid']))
    return out[:max_filled * 2]
