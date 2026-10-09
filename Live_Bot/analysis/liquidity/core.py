"""
Вынос скопления стопов и цель ЗА противоположным скоплением.

ОДНА реализация на замер и на бой — правило проекта, нарушенное однажды у
стратегии уровней и стоившее месяца недостоверных наблюдений.

ЧТО ИЩЕТСЯ ПО ПОРЯДКУ
    1. Скопления ликвидности: два и больше пивота, совпавших по цене. Это и
       есть «равные максимумы» — черта, за которую все прячут стоп.
    2. Вынос: цена уходит ЗА скопление не меньше чем на PIERCE_ATR и
       ЗАКРЫВАЕТСЯ обратно внутрь в пределах RECLAIM_BARS.
    3. Вход по рынку на баре возврата, стоп за экстремумом выноса.
    4. Цель — ЗА противоположным скоплением, а не на нём.

ЗАГЛЯДЫВАНИЯ ВПЕРЁД НЕТ, И ЭТО ГЛАВНАЯ ОПАСНОСТЬ. Пивот определяется окном
±PIVOT_BARS, то есть становится известен ЛИШЬ ЧЕРЕЗ PIVOT_BARS баров после
себя. Скопления, собранные без учёта этого, содержат пивоты из будущего, и
замер выдаёт результат, невоспроизводимый в бою.

Поэтому скопления строятся только из пивотов, подтверждённых к бару решения:
`find_pools(..., at)` не смотрит правее `at - PIVOT_BARS`. Проверка на это —
тест test_liquidity_no_lookahead.
"""

import numpy as np

from . import params

LONG, SHORT = 'LONG', 'SHORT'


def atr_series(high, low, close, period=None):
    """Средний истинный диапазон на каждом баре. NaN, пока данных не хватает."""
    period = params.ATR_PERIOD if period is None else period
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    out = np.full(n, np.nan)
    if n == 0:
        return out
    tr = np.empty(n, dtype=float)
    tr[0] = high[0] - low[0]
    if n > 1:
        prev = close[:-1]
        tr[1:] = np.maximum(high[1:] - low[1:],
                            np.maximum(np.abs(high[1:] - prev),
                                       np.abs(low[1:] - prev)))
    if n >= period:
        cum = np.cumsum(tr)
        out[period - 1] = cum[period - 1] / period
        out[period:] = (cum[period:] - cum[:-period]) / period
    return out


def pivots(high, low, bars=None):
    """
    Локальные экстремумы: (индекс, цена, сторона, когда стал известен).

    `known_at = index + bars` — момент, когда окно справа заполнилось. Без этого
    поля любая разметка молча читает будущее.
    """
    bars = params.PIVOT_BARS if bars is None else bars
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    out = []
    for i in range(bars, len(high) - bars):
        window = slice(i - bars, i + bars + 1)
        if high[i] == np.max(high[window]):
            out.append((i, float(high[i]), 'H', i + bars))
        if low[i] == np.min(low[window]):
            out.append((i, float(low[i]), 'L', i + bars))
    return out


def find_pools(pivot_list, at, atr_now, side, tolerance=None, min_touches=None,
               max_age=None):
    """
    Скопления стопов на стороне `side`, известные к бару `at`.

    Возвращает список словарей price/touches/last — от ближайшего по времени.
    Учитываются ТОЛЬКО пивоты с known_at <= at: пивот определяется окном
    ±PIVOT_BARS и до его заполнения не существует.
    """
    tolerance = (params.POOL_TOLERANCE_ATR if tolerance is None else tolerance)
    min_touches = (params.MIN_POOL_TOUCHES if min_touches is None
                   else min_touches)
    max_age = params.POOL_MAX_AGE if max_age is None else max_age
    if atr_now <= 0:
        return []

    span = tolerance * atr_now
    fresh = [p for p in pivot_list
             if p[2] == side and p[3] <= at and at - p[0] <= max_age]
    if len(fresh) < min_touches:
        return []

    pools = []
    for anchor in fresh:
        members = [p for p in fresh if abs(p[1] - anchor[1]) <= span]
        if len(members) < min_touches:
            continue
        prices = [p[1] for p in members]
        # Край скопления — самая КРАЙНЯЯ цена в нём: стопы стоят за ней, а не
        # за средней. Средняя занизила бы и цель, и глубину выноса.
        edge = max(prices) if side == 'H' else min(prices)
        pools.append({'price': edge, 'touches': len(members),
                      'last': max(p[0] for p in members)})
    # Убираем повторы: соседние опоры дают почти одно и то же скопление.
    unique, seen = [], set()
    for pool in sorted(pools, key=lambda p: -p['last']):
        key = round(pool['price'] / max(span, 1e-9))
        if key in seen:
            continue
        seen.add(key)
        unique.append(pool)
    return unique


def find_sweep(high, low, close, at, pivot_list, atr, pierce_atr=None,
               reclaim_bars=None, **pool_kwargs):
    """
    Вынос, завершившийся закрытием обратно на баре `at`. Иначе None.

    Проверяется закрытие, а не касание: тень за уровнем — это и есть сбор
    стопов, она ничего не сообщает о намерении. Значение имеет то, вернулась ли
    цена внутрь.
    """
    pierce_atr = params.PIERCE_ATR if pierce_atr is None else pierce_atr
    reclaim_bars = (params.RECLAIM_BARS if reclaim_bars is None
                    else reclaim_bars)
    atr_now = float(atr[at]) if at < len(atr) and np.isfinite(atr[at]) else 0.0
    if atr_now <= 0 or at < reclaim_bars + 2:
        return None

    need = pierce_atr * atr_now
    start = max(0, at - reclaim_bars)

    for side, direction in (('H', SHORT), ('L', LONG)):
        pools = find_pools(pivot_list, at, atr_now, side, **pool_kwargs)
        for pool in pools:
            edge = pool['price']
            if side == 'H':
                pierced = [k for k in range(start, at + 1)
                           if high[k] >= edge + need]
                inside = close[at] < edge
                extreme = max((high[k] for k in pierced), default=None)
            else:
                pierced = [k for k in range(start, at + 1)
                           if low[k] <= edge - need]
                inside = close[at] > edge
                extreme = min((low[k] for k in pierced), default=None)
            if not pierced or not inside or extreme is None:
                continue
            # Противоположное скопление — туда и целимся, точнее ЗА него.
            other = find_pools(pivot_list, at, atr_now,
                               'L' if side == 'H' else 'H', **pool_kwargs)
            if not other:
                continue
            if direction == LONG:
                ahead = [p for p in other if p['price'] > close[at]]
            else:
                ahead = [p for p in other if p['price'] < close[at]]
            if not ahead:
                continue
            # Ближайшее по цене впереди: цена идёт к следующему скоплению, а не
            # к самому далёкому.
            target_pool = min(ahead, key=lambda p: abs(p['price'] - close[at]))
            return {
                'direction': direction, 'at': int(at), 'atr': atr_now,
                'pool': pool, 'target_pool': target_pool,
                'extreme': float(extreme), 'close': float(close[at]),
                'pierce_atr': abs(extreme - edge) / atr_now,
            }
    return None


def build_trade(setup, stop_pad_atr=None, beyond_atr=None, min_rr=None,
                min_stop_pct=None):
    """
    Вход, стоп, цель. None — геометрия не годится.

    ЦЕЛЬ СТАВИТСЯ ЗА ПРОТИВОПОЛОЖНЫМ СКОПЛЕНИЕМ, а не на нём, и в этом весь
    смысл сетапа. Стопы лежат за чертой, значит и цена тянется за черту.
    Короткая цель — на самом уровне — уже дважды губила замеры: у сетки
    отношение риска к прибыли 0.38 требовало 72% попаданий, у коридора края не
    нашлось вовсе.
    """
    stop_pad_atr = (params.STOP_PAD_ATR if stop_pad_atr is None
                    else stop_pad_atr)
    beyond_atr = (params.TARGET_BEYOND_ATR if beyond_atr is None
                  else beyond_atr)
    min_rr = params.MIN_RR if min_rr is None else min_rr
    min_stop_pct = params.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct

    long_side = setup['direction'] == LONG
    atr_now = setup['atr']
    entry = setup['close']
    pad = stop_pad_atr * atr_now
    beyond = beyond_atr * atr_now
    edge = setup['target_pool']['price']

    stop = setup['extreme'] - pad if long_side else setup['extreme'] + pad
    target = edge + beyond if long_side else edge - beyond

    if long_side and not (stop < entry < target):
        return None
    if not long_side and not (target < entry < stop):
        return None

    distance = abs(entry - stop)
    floor = entry * min_stop_pct / 100
    if distance < floor:
        stop = entry - floor if long_side else entry + floor
        distance = floor
    if distance <= 0:
        return None

    rr = abs(target - entry) / distance
    if rr < min_rr:
        return None
    return {'entry': entry, 'stop': stop, 'target': target, 'rr': rr,
            'stop_pct': distance / entry * 100}
