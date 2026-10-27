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


def luxalgo_gaps(df, later=None):
    """
    Имбалансы по определению индикатора «Smart Money Concepts [LuxAlgo]»
    (TradingView, Pine v5; CC BY-NC-SA 4.0, © LuxAlgo — правило переписано с
    указанием автора, перенос и проверка — research/luxsmc). Добавлено 27.10.2026
    для стратегии SMCF; find_fvg и остальное в модуле не тронуты.

    Гэп на свече i (закрытой): бычий — low[i] > high[i-2] и close[i-1] >
    high[i-2]; медвежий — high[i] < low[i-2] и close[i-1] < low[i-2]. Средняя
    свеча должна быть сильнее порога: её тело в долях (close−open)/(open·100)
    больше удвоенного среднего модуля тех же величин с начала окна df (как
    «Auto Threshold» скрипта).

    Возвращает список гэпов как ЦЕЛЕЙ сделки:
        target_side  +1 — цель лонга (медвежий гэп над ценой), −1 — цель шорта;
        edge         ближний к цене край: у медвежьего — high[i], у бычьего — low[i];
        known_at     timestamp закрытия свечи i (с этого момента гэп известен);
        touched      цена после известности уже касалась ближнего края — по
                     свечам df после i и по свечам later (младший ТФ после
                     последней свечи df: касание внутри ещё не закрытой свечи df).

    Касание ближнего края — правило цели, а не скрипта: у скрипта бычий гэп
    снимается после полного перекрытия, медвежий — при первом касании.
    """
    n = len(df)
    if n < 3:
        return []
    o = df['open'].to_numpy(dtype=float)
    h = df['high'].to_numpy(dtype=float)
    l = df['low'].to_numpy(dtype=float)
    c = df['close'].to_numpy(dtype=float)
    ts = df['timestamp']
    step = (ts.iloc[-1] - ts.iloc[0]) / (n - 1) if n > 1 else None
    delta = np.full(n, np.nan)
    delta[1:] = (c[:-1] - o[:-1]) / (o[:-1] * 100)
    cum = np.cumsum(np.abs(np.nan_to_num(delta)))
    lh = later['high'].to_numpy(dtype=float) if later is not None and len(later) else np.empty(0)
    ll = later['low'].to_numpy(dtype=float) if later is not None and len(later) else np.empty(0)
    out = []
    for i in range(2, n):
        thr = cum[i] / i * 2
        found = []
        if l[i] > h[i - 2] and c[i - 1] > h[i - 2] and delta[i] > thr:
            found.append((-1, l[i]))
        if h[i] < l[i - 2] and c[i - 1] < l[i - 2] and -delta[i] > thr:
            found.append((1, h[i]))
        for side, edge in found:
            if side == 1:
                touched = bool((h[i + 1:] >= edge).any() or (lh >= edge).any())
            else:
                touched = bool((l[i + 1:] <= edge).any() or (ll <= edge).any())
            out.append({'target_side': side, 'edge': float(edge),
                        'known_at': ts.iloc[i] + step if step is not None else ts.iloc[i],
                        'touched': touched})
    return out
