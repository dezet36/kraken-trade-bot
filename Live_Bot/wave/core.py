"""
Волновая стратегия (Эллиотт): разметка зигзагом и построение сделки.

ОДНА реализация на замер и на бой — правило проекта, нарушенное однажды у
стратегии уровней и стоившее месяца недостоверных наблюдений.

ЧТО ИЩЕТСЯ
    1. Зигзаг: чередующиеся вершины и впадины, подтверждаемые разворотом на
       REVERSAL_ATR. Каждый пивот помнит, КОГДА он стал известен.
    2. Волна 1: колено A→B между двумя соседними пивотами, достаточно крупное.
    3. Волна 2: откат от B, не нарушающий правило 1 (не глубже A).
    4. Вход в начале волны 3, стоп за A, цель — расширение волны 1 от A.

ЗАГЛЯДЫВАНИЯ ВПЕРЁД ЗДЕСЬ НЕТ, И ЭТО ГЛАВНАЯ ОПАСНОСТЬ ВСЕЙ ЗАТЕИ. Пивот
зигзага лежит в прошлом, но становится ИЗВЕСТЕН только когда цена развернулась
на пороговое расстояние. Разметка, нарисованная по готовому графику, всегда
выглядит безупречно — именно поэтому у каждого пивота хранится `confirmed_at`,
и решение принимается на этом баре, а не на баре самого пивота.

Наивная реализация («найти локальные экстремумы в окне ±N») читает будущее и
даёт результат, который невозможно повторить в бою. Проверка на это — тест
test_wave_no_lookahead.
"""

import numpy as np

from . import params

LONG, SHORT = 'LONG', 'SHORT'
HIGH, LOW = 'H', 'L'


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


def zigzag(high, low, close, reversal_atr=None, atr=None):
    """
    Пивоты зигзага: список словарей index / price / kind / confirmed_at.

    `index` — бар, на котором стоит экстремум; `confirmed_at` — бар, на котором
    он стал известен. Разница между ними и есть цена объективности разметки.
    """
    reversal_atr = params.REVERSAL_ATR if reversal_atr is None else reversal_atr
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if atr is None:
        atr = atr_series(high, low, close)

    n = len(close)
    pivots = []
    ready = np.flatnonzero(np.isfinite(atr) & (atr > 0))
    if n == 0 or len(ready) == 0:
        return pivots

    start = int(ready[0])
    hi_i, hi_p = start, float(high[start])
    lo_i, lo_p = start, float(low[start])
    up = None                       # направление ещё не определено

    def add(index, price, kind, at):
        pivots.append({'index': int(index), 'price': float(price),
                       'kind': kind, 'confirmed_at': int(at)})

    for i in range(start, n):
        need = reversal_atr * atr[i]
        if not np.isfinite(need) or need <= 0:
            continue
        if high[i] > hi_p:
            hi_i, hi_p = i, float(high[i])
        if low[i] < lo_p:
            lo_i, lo_p = i, float(low[i])

        if up is None:
            # Первое движение задаёт направление: чей порог сломан раньше, тот
            # экстремум и становится первым пивотом.
            if hi_i < i and hi_p - low[i] >= need:
                add(hi_i, hi_p, HIGH, i)
                up, lo_i, lo_p = False, i, float(low[i])
            elif lo_i < i and high[i] - lo_p >= need:
                add(lo_i, lo_p, LOW, i)
                up, hi_i, hi_p = True, i, float(high[i])
        elif up:
            if hi_p - low[i] >= need:
                add(hi_i, hi_p, HIGH, i)
                up, lo_i, lo_p = False, i, float(low[i])
        else:
            if high[i] - lo_p >= need:
                add(lo_i, lo_p, LOW, i)
                up, hi_i, hi_p = True, i, float(high[i])

    return pivots


def find_wave(pivots, k, atr, entry_mode=None, min_wave_atr=None,
              min_leg_ratio=None, min_retrace=None, max_retrace=None):
    """
    Волна, опирающаяся на пивот номер `k` как на последний известный. None —
    разметка не складывается.

    В режиме 'limit' пивот k — это конец волны 1 (точка B): волна 2 ещё идёт, и
    заявка ставится на её ожидаемую глубину. В режиме 'market' пивот k — это уже
    подтверждённое дно волны 2 (точка C), и вход происходит по рынку.

    Решение принимается на баре `at` = confirmed_at пивота k. Ни одно поле
    результата не читает данные после этого бара.
    """
    entry_mode = params.ENTRY_MODE if entry_mode is None else entry_mode
    min_wave_atr = params.MIN_WAVE_ATR if min_wave_atr is None else min_wave_atr
    min_leg_ratio = (params.MIN_LEG_RATIO if min_leg_ratio is None
                     else min_leg_ratio)
    min_retrace = params.MIN_RETRACE if min_retrace is None else min_retrace
    max_retrace = params.MAX_RETRACE if max_retrace is None else max_retrace

    if entry_mode == 'market':
        need_back, c_index = 2, k
    else:
        need_back, c_index = 1, None
    if k < need_back:
        return None

    a = pivots[k - need_back]
    b = pivots[k - need_back + 1]
    c = pivots[c_index] if c_index is not None else None

    # Зигзаг чередует стороны по построению, но опираться на это молча нельзя:
    # любая будущая правка порядка пивотов сломает разметку беззвучно.
    if a['kind'] == b['kind']:
        return None
    if c is not None and c['kind'] != a['kind']:
        return None

    direction = LONG if a['kind'] == LOW else SHORT
    wave1 = abs(b['price'] - a['price'])
    if wave1 <= 0:
        return None

    at = (c or b)['confirmed_at']
    atr_now = float(atr[at]) if at < len(atr) and np.isfinite(atr[at]) else 0.0
    if atr_now <= 0:
        return None
    if wave1 < min_wave_atr * atr_now:
        return None

    # Волна 1 крупнее предыдущего колена — механический заменитель фразы «это
    # начало новой последовательности, а не её продолжение».
    prev_leg = None
    if k - need_back >= 1:
        prev = pivots[k - need_back - 1]
        prev_leg = abs(a['price'] - prev['price'])
        if min_leg_ratio > 0 and prev_leg > 0 and wave1 < min_leg_ratio * prev_leg:
            return None

    retrace = None
    if c is not None:
        # Правило 1: откат глубже начала волны 1 отменяет разметку целиком.
        if direction == LONG and c['price'] <= a['price']:
            return None
        if direction == SHORT and c['price'] >= a['price']:
            return None
        depth = (b['price'] - c['price']) if direction == LONG else (c['price'] - b['price'])
        retrace = depth / wave1
        if not (min_retrace <= retrace <= max_retrace):
            return None

    return {'direction': direction, 'a': a, 'b': b, 'c': c, 'at': int(at),
            'wave1': wave1, 'wave1_atr': wave1 / atr_now, 'atr': atr_now,
            'retrace': retrace, 'prev_leg': prev_leg,
            'lag': int(at - (c or b)['index']), 'entry_mode': entry_mode}


def build_trade(wave, price_now, entry_retrace=None, target_ext=None,
                stop_pad_atr=None, min_rr=None, min_stop_pct=None):
    """
    Вход, стоп, цель. None — геометрия не годится.

    Стоп стоит за началом волны 1 не потому, что там «удобно», а потому что
    правило 1 объявляет разметку недействительной ровно за этой чертой. Это
    единственный стоп во всём проекте, который следует из самой идеи сетапа, а
    не назначается по волатильности.

    `price_now` — цена на баре решения, и она обязательна. Без неё замер даёт
    бесплатный обед, причём тихо:

        · пивот подтверждается разворотом на REVERSAL_ATR, значит к моменту
          подтверждения волны 1 цена УЖЕ откатила на это расстояние. При пороге
          2.5 ATR и медианном колене 4.1 ATR это 61% отката — лимит на уровне
          50% ставить уже некуда, цена его прошла. Движок нальёт такую заявку
          по её цене, то есть лучше рынка;
        · то же и со входом «по рынку на дне волны 2»: дно известно только
          после разворота от него, и войти по его цене нельзя.

    Поэтому лимит отвергается, если уровень уже пройден, а вход по рынку берёт
    цену бара решения — с ухудшением, которое и есть плата за подтверждённую
    разметку.
    """
    entry_retrace = (params.ENTRY_RETRACE if entry_retrace is None
                     else entry_retrace)
    target_ext = params.TARGET_EXT if target_ext is None else target_ext
    stop_pad_atr = (params.STOP_PAD_ATR if stop_pad_atr is None
                    else stop_pad_atr)
    min_rr = params.MIN_RR if min_rr is None else min_rr
    min_stop_pct = params.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct

    a, b, c = wave['a'], wave['b'], wave['c']
    long_side = wave['direction'] == LONG
    pad = stop_pad_atr * wave['atr']

    price_now = float(price_now)
    if c is not None:
        entry = price_now           # по рынку, на баре подтверждения дна
    elif long_side:
        entry = b['price'] - wave['wave1'] * entry_retrace
        if entry >= price_now:      # уровень уже пройден — заявке некуда встать
            return None
    else:
        entry = b['price'] + wave['wave1'] * entry_retrace
        if entry <= price_now:
            return None

    stop = a['price'] - pad if long_side else a['price'] + pad
    target = (a['price'] + wave['wave1'] * target_ext if long_side
              else a['price'] - wave['wave1'] * target_ext)

    # Вход за пределами волны 1: снизу это уже нарушенное правило 1, сверху —
    # погоня за начавшейся волной 3 со стопом во всю её длину.
    if long_side and not (stop < entry < b['price']):
        return None
    if not long_side and not (b['price'] < entry < stop):
        return None

    distance = abs(entry - stop)
    floor = entry * min_stop_pct / 100
    if distance < floor:
        stop = entry - floor if long_side else entry + floor
        distance = floor
    if distance <= 0:
        return None
    if (target - entry > 0) != long_side:
        return None

    rr = abs(target - entry) / distance
    if rr < min_rr:
        return None
    return {'entry': entry, 'stop': stop, 'target': target, 'rr': rr,
            'stop_pct': distance / entry * 100}
