"""
Имбаланс / Fair Value Gap (§4 методички).

Имбаланс — диапазон, где ликвидность предлагалась только одной стороне рынка,
а для другой цена была неэффективна. Определяется по ТРЁМ закрытым свечам:
диапазон между тенями первой и третьей.

    Бычий FVG   — high[i-2] < low[i]:   ликвидность была только на покупку
    Медвежий FVG — low[i-2] > high[i]:  только на продажу

Алгоритм рынка стремится заполнить имбаланс — это магнит для цены (§4.1).
§4.2: чаще всего заполняется 50% или 100% гэпа; полное заполнение (FF, Full
Fill) — это возврат к максимуму первой свечи для бычьего FVG и к минимуму
первой свечи для медвежьего.
"""

import numpy as np

from . import params

BULLISH = 'BULLISH'
BEARISH = 'BEARISH'


def find_fvg(df, min_size_pct=None):
    """
    Находит все имбалансы на серии свечей.

    Возвращает список словарей:
        {'direction', 'top', 'bottom', 'mid', 'index', 'confirmed_at', 'size_pct'}

    index        — индекс ТРЕТЬЕЙ свечи (гэп существует начиная с неё)
    confirmed_at — та же свеча: FVG виден сразу, как только она закрылась,
                   поэтому подглядывания в будущее здесь нет.
    """
    min_size_pct = params.FVG_MIN_SIZE_PCT if min_size_pct is None else min_size_pct

    size = len(df)
    if size < 3:
        return []

    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    times = df['timestamp'].to_numpy() if 'timestamp' in df.columns else np.arange(size)

    first_high = high[:-2]     # свеча i-2
    first_low = low[:-2]
    third_high = high[2:]      # свеча i
    third_low = low[2:]
    idx = np.arange(2, size)

    gaps = []

    bull = third_low > first_high
    for k in np.flatnonzero(bull):
        bottom, top = float(first_high[k]), float(third_low[k])
        span = (top - bottom) / top if top else 0.0
        if span < min_size_pct:
            continue
        gaps.append({
            'direction': BULLISH,
            'bottom': bottom,
            'top': top,
            'mid': (top + bottom) / 2,
            'index': int(idx[k]),
            'confirmed_at': int(idx[k]),
            'time': times[idx[k]],
            'size_pct': span,
        })

    bear = first_low > third_high
    for k in np.flatnonzero(bear):
        bottom, top = float(third_high[k]), float(first_low[k])
        span = (top - bottom) / top if top else 0.0
        if span < min_size_pct:
            continue
        gaps.append({
            'direction': BEARISH,
            'bottom': bottom,
            'top': top,
            'mid': (top + bottom) / 2,
            'index': int(idx[k]),
            'confirmed_at': int(idx[k]),
            'time': times[idx[k]],
            'size_pct': span,
        })

    return sorted(gaps, key=lambda g: g['index'])


def fill_state(df, gap, at_index):
    """
    Насколько имбаланс заполнен к свече `at_index`.

    Возвращает долю 0.0-1.0, где 1.0 = полное заполнение (FF, §4.2):
    цена дошла до дальней границы гэпа — максимума первой свечи для бычьего
    и минимума первой свечи для медвежьего.
    """
    start = gap['index'] + 1
    if at_index < start:
        return 0.0

    high = df['high'].to_numpy(dtype=float)[start:at_index + 1]
    low = df['low'].to_numpy(dtype=float)[start:at_index + 1]
    if len(high) == 0:
        return 0.0

    span = gap['top'] - gap['bottom']
    if span <= 0:
        return 1.0

    if gap['direction'] == BULLISH:
        # Заполняется сверху вниз: чем ниже опустилась цена, тем больше заполнено
        deepest = float(low.min())
        filled = (gap['top'] - deepest) / span
    else:
        highest = float(high.max())
        filled = (highest - gap['bottom']) / span

    return float(min(max(filled, 0.0), 1.0))


def active_fvgs(df, gaps, at_index, direction=None,
                max_age=None, mitigated_at=None):
    """
    Имбалансы, актуальные на свече `at_index`: уже сформированы, ещё не
    заполнены сверх порога и не устарели.

    §5: ранее протестированные зоны, как правило, повторной реакции не дают,
    поэтому заполненные глубже FVG_MITIGATED_AT отбрасываем.
    """
    max_age = params.FVG_MAX_AGE_BARS if max_age is None else max_age
    mitigated_at = params.FVG_MITIGATED_AT if mitigated_at is None else mitigated_at

    out = []
    for gap in gaps:
        if gap['confirmed_at'] > at_index:
            continue
        if at_index - gap['index'] > max_age:
            continue
        if direction and gap['direction'] != direction:
            continue
        filled = fill_state(df, gap, at_index)
        if filled >= mitigated_at:
            continue
        out.append({**gap, 'filled': filled})

    return out


def nearest_fvg(gaps, price, direction=None, max_distance_pct=0.02):
    """
    Ближайший к цене имбаланс — используется как фактор confluence: имбаланс
    рядом с зоной интереса заметно повышает вероятность реакции (§4.2, §5.1).
    """
    best = None
    best_dist = None

    for gap in gaps:
        if direction and gap['direction'] != direction:
            continue
        if gap['bottom'] <= price <= gap['top']:
            dist = 0.0
        else:
            dist = min(abs(price - gap['top']), abs(price - gap['bottom'])) / price
        if dist > max_distance_pct:
            continue
        if best_dist is None or dist < best_dist:
            best, best_dist = gap, dist

    return best
