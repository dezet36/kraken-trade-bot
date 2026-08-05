"""
Ядро стратегии уровней: чистые вычисления, без биржи и ордеров.

Модуль намеренно ничего не знает ни про ccxt, ни про trade_manager: на вход
свечи, на выход сетап. Ровно та же функция вызывается из бэктеста и из
живого бота, поэтому расхождение между моделью и боем невозможно —
единственный способ его исключить, известный по опыту этого проекта.

ЧЕСТНОСТЬ ПО ВРЕМЕНИ. Экстремум становится известен через PIVOT_N баров
после себя; уровень — с подтверждения последнего входящего в него касания.
Прокол и возврат ищутся только по ЗАКРЫТЫМ свечам, вход считается по
закрытию свечи возврата. Ни одна величина не смотрит вперёд.
"""

import numpy as np

from . import params

LONG, SHORT = 'LONG', 'SHORT'


def atr(high, low, close, period=14):
    """Средний истинный диапазон, сглаживание Уайлдера."""
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    out = np.full(len(tr), np.nan)
    if len(tr) > period:
        out[period] = tr[1:period + 1].mean()
        for i in range(period + 1, len(tr)):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def pivots(high, low, n=None):
    """
    Точки касания: локальные экстремумы с n барами по обе стороны.

    known_at — бар, на котором экстремум становится известен. Всё, что
    строится по нему раньше этого момента, знает будущее.
    """
    n = params.PIVOT_N if n is None else n
    out = []
    for i in range(n, len(high) - n):
        wh = high[i - n:i + n + 1]
        wl = low[i - n:i + n + 1]
        if high[i] == wh.max() and wh.argmax() == n:
            out.append({'index': i, 'price': float(high[i]), 'kind': 'high',
                        'known_at': i + n})
        if low[i] == wl.min() and wl.argmin() == n:
            out.append({'index': i, 'price': float(low[i]), 'kind': 'low',
                        'known_at': i + n})
    return sorted(out, key=lambda p: p['index'])


def build_levels(high, low, tolerance_pct=None, min_touches=None):
    """
    Уровни: кластеры касаний на одной цене.

    Вершины и низы кладутся в ОДИН пул: уровень, который сначала
    останавливал рост, а потом держал падение, раздельные пулы не увидят.
    (Замер показал, что зеркальность результат не улучшает, но объединённый
    пул даёт больше уровней вообще, и это полезно само по себе.)
    """
    tolerance_pct = params.TOLERANCE_PCT if tolerance_pct is None else tolerance_pct
    min_touches = params.MIN_TOUCHES if min_touches is None else min_touches

    points = pivots(high, low)
    levels, used = [], set()
    for i, first in enumerate(points):
        if i in used:
            continue
        members, idxs = [first], {i}
        for j in range(i + 1, len(points)):
            second = points[j]
            if second['index'] - first['index'] > params.MAX_SPAN_BARS:
                break
            if abs(second['price'] - first['price']) / first['price'] * 100 <= tolerance_pct:
                members.append(second)
                idxs.add(j)
        if len(members) < min_touches:
            continue
        used |= idxs
        levels.append({
            'price': float(np.mean([m['price'] for m in members])),
            'touches': len(members),
            'mirror': len({m['kind'] for m in members}) > 1,
            'known_at': max(m['known_at'] for m in members),
        })
    return levels


def find_reclaim(high, low, close, start, level, side, atr_now):
    """
    Прокол уровня с возвратом. Возвращает (индекс возврата, экстремум) или None.

    Это главный элемент стратегии. Без него вход происходит и когда уровень
    устоял, и когда его прошли насквозь — в момент постановки заявки это
    неразличимо, и результат превращается в подбрасывание монеты с
    комиссией. Замер: -1104 R без подтверждения против +138 R с ним.
    """
    limit = min(start + params.RECLAIM_BARS + 1, len(close))
    need = params.PIERCE_ATR * atr_now

    pierce_at = None
    for k in range(start, limit):
        if side == LONG and low[k] <= level - need:
            pierce_at = k
            break
        if side == SHORT and high[k] >= level + need:
            pierce_at = k
            break
    if pierce_at is None:
        return None

    extreme = low[pierce_at] if side == LONG else high[pierce_at]
    for k in range(pierce_at, min(pierce_at + params.RECLAIM_BARS + 1, len(close))):
        extreme = min(extreme, low[k]) if side == LONG else max(extreme, high[k])
        if k > pierce_at and (close[k] > level if side == LONG else close[k] < level):
            return k, float(extreme)
    return None


def evaluate(high, low, close, volume, at_index, levels=None, atr_values=None):
    """
    Сетап на свече at_index или (None, причина отказа).

    Причина возвращается всегда: без неё воронка отсева на дашборде
    показывала бы «ничего не найдено» без объяснения, а это самый частый
    вопрос при наблюдении за ботом.
    """
    i = at_index
    if i < 60 or i >= len(close):
        return None, 'мало истории'

    a = atr(high, low, close) if atr_values is None else atr_values
    if np.isnan(a[i]) or a[i] <= 0:
        return None, 'ATR не посчитан'

    if levels is None:
        levels = build_levels(high, low)
    known = [lv for lv in levels
             if lv['known_at'] <= i and i - lv['known_at'] <= params.MAX_AGE_BARS]
    if not known:
        return None, 'нет подтверждённых уровней'

    price = float(close[i])
    known.sort(key=lambda lv: abs(lv['price'] - price))

    reason = 'цена далеко от уровней'
    for lv in known[:params.NEAREST_LEVELS]:
        gap = price - lv['price']
        if abs(gap) > params.TRIGGER_ATR * a[i]:
            continue
        if abs(gap) < params.MIN_GAP_ATR * a[i]:
            reason = 'цена вплотную к уровню — вход поздно'
            continue

        side = LONG if gap > 0 else SHORT   # выше уровня -> он поддержка

        found = find_reclaim(high, low, close, i, lv['price'], side, a[i])
        if found is None:
            reason = 'нет подтверждения: уровень не проколот с возвратом'
            continue
        r_at, extreme = found

        avg = float(np.mean(volume[max(0, r_at - params.VOLUME_WINDOW):r_at])) \
            if r_at > 0 else 0.0
        ratio = (volume[r_at] / avg) if avg > 0 else 0.0
        if ratio < params.VOLUME_RATIO:
            reason = (f'объём на возврате {ratio:.1f}x < '
                      f'{params.VOLUME_RATIO}x — уровень никто не защищает')
            continue

        entry = float(close[r_at])
        dist = max(abs(entry - extreme) + params.STOP_PAD_ATR * a[r_at],
                   entry * params.MIN_STOP_PCT / 100)
        stop = entry - dist if side == LONG else entry + dist

        # Цель — следующий уровень по ходу сделки. Так выходит трейдер,
        # торгующий от уровней: движение живёт до следующего препятствия.
        ahead = [lv2['price'] for lv2 in known
                 if (lv2['price'] > entry + dist if side == LONG
                     else lv2['price'] < entry - dist)]
        target = None
        if ahead:
            candidate = min(ahead) if side == LONG else max(ahead)
            if abs(candidate - entry) / dist >= params.MIN_TARGET_R:
                target = candidate
        if target is None:
            target = (entry + params.FALLBACK_RR * dist if side == LONG
                      else entry - params.FALLBACK_RR * dist)

        return {
            'direction': side,
            'level': float(lv['price']),
            'touches': lv['touches'],
            'mirror': lv['mirror'],
            'entry': entry,
            'stop_loss': float(stop),
            'target': float(target),
            'rr': float(abs(target - entry) / dist),
            'sl_distance': float(dist),
            'volume_ratio': float(ratio),
            'reclaim_index': int(r_at),
            'pierce_extreme': float(extreme),
        }, None

    return None, reason
