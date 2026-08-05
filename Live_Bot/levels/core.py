"""
Ядро стратегии уровней: чистые вычисления, без биржи и ордеров.

Модуль намеренно ничего не знает ни про ccxt, ни про trade_manager: на вход
свечи, на выход сетап. Ровно эта функция вызывается и из бэктеста, и из
живого бота — иначе модель и бой расходятся, и в этом проекте такое уже
стоило девяти кругов подгонки под несуществующее преимущество.

НАПРАВЛЕНИЕ ПОИСКА — не деталь реализации. Свеча at_index, на которой
принимается решение, это свеча ВОЗВРАТА. Прокол ищется НАЗАД, в предыдущих
RECLAIM_BARS свечах. Первая версия искала вперёд: в бэктесте работало
(ордер создавался на баре возврата, лежащем в прошлом), а живой бот зовёт
функцию на последней закрытой свече, где никакого «вперёд» нет — и сигнал
не появлялся никогда. Замер показывал +90%, бот молчал, и по логу это
выглядело как «сетапов не найдено».

ЧЕСТНОСТЬ ПО ВРЕМЕНИ. Экстремум становится известен через PIVOT_N баров
после себя; уровень — с подтверждения последнего входящего в него касания.
Ни одна проверка не смотрит правее at_index.
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


def find_reclaim(high, low, close, at, level, side, atr_now):
    """
    Завершился ли на свече `at` прокол уровня с возвратом.

    Возвращает (индекс первой свечи прокола, экстремум прокола) или None.
    Смотрит только назад: свеча `at` обязана закрыться обратно за уровнем, а
    прокол должен был случиться в предыдущих RECLAIM_BARS свечах.

    Подтверждение — главный элемент стратегии. Без него вход происходит и
    когда уровень устоял, и когда его прошли насквозь: в момент постановки
    заявки это неразличимо, и результат превращается в подбрасывание монеты
    с комиссией. Замер: -1104 R без подтверждения против +138 R с ним.
    """
    if at <= 0 or at >= len(close):
        return None
    back = close[at] > level if side == LONG else close[at] < level
    if not back:
        return None

    need = params.PIERCE_ATR * atr_now
    start = max(0, at - params.RECLAIM_BARS)
    pierce_at, extreme = None, None
    for k in range(start, at):
        if side == LONG and low[k] <= level - need:
            pierce_at = k if pierce_at is None else pierce_at
            extreme = low[k] if extreme is None else min(extreme, low[k])
        elif side == SHORT and high[k] >= level + need:
            pierce_at = k if pierce_at is None else pierce_at
            extreme = high[k] if extreme is None else max(extreme, high[k])
    if pierce_at is None:
        return None

    # Тень самой свечи возврата тоже входит в экстремум: стоп должен стоять
    # за всей зоной, куда цена сходила.
    extreme = min(extreme, low[at]) if side == LONG else max(extreme, high[at])
    return pierce_at, float(extreme)


def evaluate(high, low, close, volume, at_index, levels=None, atr_values=None):
    """
    Сетап, ЗАВЕРШИВШИЙСЯ на свече at_index, или (None, причина отказа).

    at_index — свеча возврата, на закрытии которой принимается решение.
    Живой бот подаёт последнюю закрытую свечу, бэктест перебирает все по
    очереди; видят они при этом ровно одно и то же.

    Причина отказа возвращается всегда: без неё воронка отсева на дашборде
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
        if abs(price - lv['price']) > params.TRIGGER_ATR * a[i]:
            continue

        # Сторона определяется тем, куда пошёл возврат: проткнули снизу и
        # закрылись выше — уровень устоял как поддержка, покупаем.
        side = LONG if price > lv['price'] else SHORT

        found = find_reclaim(high, low, close, i, lv['price'], side, a[i])
        if found is None:
            reason = 'нет подтверждения: уровень не проколот с возвратом'
            continue
        pierce_at, extreme = found

        # Подход проверяется на свече ПЕРЕД проколом: цена должна была идти
        # к уровню со своей стороны, а не топтаться на нём. Без этого
        # сетапом считался бы любой возврат внутри болтанки вокруг уровня.
        before = pierce_at - 1
        if before < 1:
            reason = 'мало истории до прокола'
            continue
        approach = close[before] - lv['price']
        if (approach <= 0) if side == LONG else (approach >= 0):
            reason = 'перед проколом цена была не с той стороны уровня'
            continue
        if abs(approach) < params.MIN_GAP_ATR * a[before]:
            reason = 'перед проколом цена уже стояла на уровне'
            continue
        if abs(approach) > params.TRIGGER_ATR * a[before]:
            reason = 'перед проколом цена была далеко от уровня'
            continue

        avg = float(np.mean(volume[max(0, i - params.VOLUME_WINDOW):i])) if i > 0 else 0.0
        ratio = (volume[i] / avg) if avg > 0 else 0.0
        if ratio < params.VOLUME_RATIO:
            reason = (f'объём на возврате {ratio:.1f}x < {params.VOLUME_RATIO}x '
                      f'— уровень никто не защищает')
            continue

        entry = price
        dist = max(abs(entry - extreme) + params.STOP_PAD_ATR * a[i],
                   entry * params.MIN_STOP_PCT / 100)
        stop = entry - dist if side == LONG else entry + dist

        # Цель — следующий уровень по ходу сделки. Так выходит трейдер,
        # торгующий от уровней: движение живёт до следующего препятствия.
        # Запасной цели, кратной риску, здесь нет намеренно: замер показал,
        # что цель на уровне работает только вместе с объёмом, а фиксированная
        # кратность — это уже другая стратегия с другим результатом.
        ahead = [other['price'] for other in known
                 if (other['price'] > entry + dist if side == LONG
                     else other['price'] < entry - dist)]
        if not ahead:
            reason = 'впереди нет уровня для цели'
            continue
        target = min(ahead) if side == LONG else max(ahead)
        if abs(target - entry) / dist < params.MIN_TARGET_R:
            reason = f'следующий уровень ближе {params.MIN_TARGET_R}R'
            continue

        return {
            'direction': side,
            'level': float(lv['price']),
            'touches': lv['touches'],
            'mirror': lv['mirror'],
            'entry': float(entry),
            'stop_loss': float(stop),
            'target': float(target),
            'rr': float(abs(target - entry) / dist),
            'sl_distance': float(dist),
            'volume_ratio': float(ratio),
            'reclaim_index': int(i),
            'pierce_index': int(pierce_at),
            'pierce_extreme': float(extreme),
        }, None

    return None, reason
