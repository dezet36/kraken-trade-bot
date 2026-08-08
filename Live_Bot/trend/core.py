"""
Пробой канала Дончиана на длинном горизонте: поиск и геометрия.

ОДНА реализация на замер и на бой — правило проекта, нарушенное однажды у
стратегии уровней и стоившее месяца недостоверных наблюдений.

ЧТО ИЩЕТСЯ
    1. Канал: максимум и минимум последних CHANNEL баров, СТРОГО ДО текущего.
    2. Пробой: закрытие ушло за границу не меньше чем на BREAK_ATR.
    3. Стоп в ATR от входа, выход по противоположному каналу либо по цели.

КАНАЛ СЧИТАЕТСЯ БЕЗ ТЕКУЩЕГО БАРА, И ЭТО НЕ ПРИДИРКА. Включив текущий бар, мы
получили бы, что закрытие никогда не выходит за собственный максимум — условие
пробоя стало бы невыполнимым, — либо, при сравнении с максимумом ВКЛЮЧАЯ себя,
тривиально выполнимым. Ровно такая ошибка по построению убила первую версию
замера ложного пробоя: граница коридора вбирала в себя прокол, и условие
становилось неисполнимым. Здесь окно кончается на баре `at - 1`.
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


def channels(high, low, length):
    """
    Границы канала на каждом баре, посчитанные по барам СТРОГО ДО него.

    Возвращает (верх, низ). Сдвиг на один бар обязателен: канал, включающий
    текущий бар, делает условие пробоя либо невыполнимым, либо тривиальным.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    n = len(high)
    top = np.full(n, np.nan)
    bottom = np.full(n, np.nan)
    for i in range(length, n):
        window = slice(i - length, i)      # без текущего бара
        top[i] = np.max(high[window])
        bottom[i] = np.min(low[window])
    return top, bottom


def find_break(close, at, top, bottom, atr, break_atr=None, on_close=None):
    """Пробой канала на баре `at` либо None."""
    break_atr = params.BREAK_ATR if break_atr is None else break_atr
    on_close = params.BREAK_ON_CLOSE if on_close is None else on_close
    if at >= len(close) or not np.isfinite(top[at]) or not np.isfinite(bottom[at]):
        return None
    atr_now = float(atr[at]) if np.isfinite(atr[at]) else 0.0
    if atr_now <= 0:
        return None

    need = break_atr * atr_now
    price = float(close[at])
    if price >= top[at] + need:
        direction, edge = LONG, float(top[at])
    elif price <= bottom[at] - need:
        direction, edge = SHORT, float(bottom[at])
    else:
        return None
    return {'direction': direction, 'at': int(at), 'atr': atr_now,
            'edge': edge, 'close': price,
            'width_atr': (top[at] - bottom[at]) / atr_now}


def build_trade(setup, exit_top, exit_bottom, stop_atr=None, exit_mode=None,
                target_r=None, min_stop_pct=None):
    """
    Вход, стоп, цель. None — геометрия не годится.

    ВЫХОД ПО ПРОТИВОПОЛОЖНОМУ КАНАЛУ — классика жанра, и здесь он основной.
    Смысл следования за трендом в том, что редкие крупные движения окупают
    частые мелкие убытки; фиксированная цель срезает именно те сделки, ради
    которых всё затевается. Замер выходов на четырёх стратегиях это подтвердил:
    резать рано вредило всем, давать дойти помогало.

    Движок умеет только фиксированные цели, поэтому «противоположный канал»
    переводится в цену на баре входа. Это приближение: настоящий канал ползёт
    вместе с ценой. Приближение КОНСЕРВАТИВНОЕ — неподвижная цель ближе, чем
    уползающая в сторону прибыли, — и потому занижает результат, а не завышает.
    """
    stop_atr = params.STOP_ATR if stop_atr is None else stop_atr
    exit_mode = params.EXIT_MODE if exit_mode is None else exit_mode
    target_r = params.TARGET_R if target_r is None else target_r
    min_stop_pct = params.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct

    long_side = setup['direction'] == LONG
    entry = setup['close']
    distance = stop_atr * setup['atr']
    floor = entry * min_stop_pct / 100
    distance = max(distance, floor)
    if distance <= 0:
        return None
    stop = entry - distance if long_side else entry + distance

    if exit_mode == 'channel':
        edge = exit_bottom if long_side else exit_top
        if edge is None or not np.isfinite(edge):
            return None
        target = float(edge)
        # Противоположный канал на баре входа лежит ПОЗАДИ цены — он не может
        # служить целью. Разворачиваем его в сторону движения на ту же ширину:
        # это и есть «дойти до другого края».
        reach = abs(entry - target)
        target = entry + reach if long_side else entry - reach
    else:
        target = (entry + target_r * distance if long_side
                  else entry - target_r * distance)

    if (target - entry > 0) != long_side:
        return None
    rr = abs(target - entry) / distance
    if rr <= 0:
        return None
    return {'entry': entry, 'stop': stop, 'target': target, 'rr': rr,
            'stop_pct': distance / entry * 100}
