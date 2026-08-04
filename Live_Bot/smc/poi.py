"""
Зоны интереса — Point Of Interest (§5 методички).

POI — область потенциального разворота, где открывается позиция. Типы:

    Order Block (§5.1)      — свеча, показывающая набор позиции умным
                              капиталом. Критерии: снятие ликвидности +
                              поглощение; дополнительно — тест зоны
                              поддержки/сопротивления, имбаланс, слом структуры.
    Breaker Block (§5.2)    — импульсно пробитый Order Block после
                              формирования нового HH/LL. Пробитое
                              сопротивление становится поддержкой.
    Mitigation Block (§5.3) — импульсно пробитый максимум/минимум после
                              структурных LH/HL. Отличие от брейкера —
                              ОТСУТСТВИЕ снятия ликвидности (основа — SMS).
    Wick (§5.4)             — тень свечи, обновившей ликвидность. Логика
                              идентична Order Block, отличие визуальное.

Триггерные точки внутри зоны (§5.1):
    первая  — ближняя граница зоны (лучшая реакция цены);
    вторая  — 50% зоны.
Стоп — за экстремумом зоны.

Важное правило §5: «ранее протестированные POI, как правило, не дают
повторной реакции» — отсюда учёт числа касаний и отбор свежих зон.
"""

import numpy as np

from . import params, structure as structure_mod

BULLISH = 'BULLISH'
BEARISH = 'BEARISH'


def _zone_geometry(direction, top, bottom):
    """
    Триггерные точки и уровень инвалидации зоны.

    Для бычьей зоны (поддержка) цена приходит сверху: ближняя граница — верх
    зоны, стоп — под низом. Для медвежьей всё зеркально.
    """
    mid = (top + bottom) / 2
    if direction == BULLISH:
        return {'entry_near': top, 'entry_mid': mid, 'invalidation': bottom}
    return {'entry_near': bottom, 'entry_mid': mid, 'invalidation': top}


def count_touches(df, zone, from_index, to_index):
    """
    Сколько раз цена заходила в зону после её формирования.

    Считаем именно ЗАХОДЫ (пересечения границы), а не свечи внутри: серия
    свечей внутри зоны — это один тест, а не десять.
    """
    if to_index <= from_index:
        return 0

    high = df['high'].to_numpy(dtype=float)[from_index + 1:to_index + 1]
    low = df['low'].to_numpy(dtype=float)[from_index + 1:to_index + 1]
    if len(high) == 0:
        return 0

    inside = (low <= zone['top']) & (high >= zone['bottom'])
    if not inside.any():
        return 0
    # Переход False -> True = новый заход в зону
    entries = int(inside[0]) + int(np.count_nonzero(inside[1:] & ~inside[:-1]))
    return entries


def find_order_blocks(df, structure_obj, min_impulse_pct=None,
                      max_lookback=20):
    """
    Order Block, привязанный к слому структуры.

    Ищем не «любую свечу подходящего цвета», а ту, из которой вышел импульс,
    сломавший структуру: методичка §5.1 прямо называет слом структуры
    (bos/mbos) критерием формирования, а §5.1 добавляет, что после поглощения
    ОБ ожидается имбаланс и слом структуры — «подтверждение того, что умный
    капитал действительно наращивает позицию».

    Для бычьего слома берём ПОСЛЕДНЮЮ медвежью свечу перед импульсом —
    это и есть блок покупок умного капитала.
    """
    min_impulse_pct = (params.OB_MIN_IMPULSE_PCT if min_impulse_pct is None
                       else min_impulse_pct)

    open_ = df['open'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    times = df['timestamp'].to_numpy() if 'timestamp' in df.columns else np.arange(len(df))

    blocks = []
    for event in structure_obj['events']:
        brk = event['index']
        direction = event['direction']
        start = max(0, brk - max_lookback)

        # Последняя свеча противоположного цвета перед сломом
        ob_idx = None
        for i in range(brk - 1, start - 1, -1):
            is_bear = close[i] < open_[i]
            if (direction == BULLISH and is_bear) or (direction == BEARISH and not is_bear):
                ob_idx = i
                break
        if ob_idx is None:
            continue

        # Импульс от блока до точки слома должен быть значимым
        if direction == BULLISH:
            impulse = (high[brk] - low[ob_idx]) / low[ob_idx] if low[ob_idx] else 0.0
        else:
            impulse = (high[ob_idx] - low[brk]) / high[ob_idx] if high[ob_idx] else 0.0
        if impulse < min_impulse_pct:
            continue

        top, bottom = float(high[ob_idx]), float(low[ob_idx])
        if top <= bottom:
            continue

        blocks.append({
            'type': 'ORDER_BLOCK',
            'direction': direction,
            'top': top,
            'bottom': bottom,
            'index': ob_idx,
            # Зона становится торгуемой только после слома, который её подтвердил
            'confirmed_at': brk,
            'time': times[ob_idx],
            'impulse_pct': float(impulse),
            'break_event': event['type'],
            'break_index': brk,
            **_zone_geometry(direction, top, bottom),
        })

    return blocks


def find_breaker_blocks(df, structure_obj, order_blocks, max_age=None):
    """
    Breaker Block (§5.2) — импульсно пробитый Order Block.

    Механика: бычий брейкер получается, когда цена импульсно пробивает
    МЕДВЕЖИЙ order block; пробитое сопротивление становится поддержкой.
    Методичка: «Бычий брейкер подтверждается и становится зоной поддержки в
    момент импульсного пробоя медвежьего ОБ».
    """
    max_age = params.POI_MAX_AGE_BARS if max_age is None else max_age

    close = df['close'].to_numpy(dtype=float)
    size = len(df)
    breakers = []

    for block in order_blocks:
        start = block['confirmed_at'] + 1
        if start >= size:
            continue
        stop = min(start + max_age, size)
        window = close[start:stop]
        if len(window) == 0:
            continue

        # Медвежий OB пробит вверх -> бычий брейкер (и наоборот)
        if block['direction'] == BEARISH:
            broken = window > block['top']
            new_direction = BULLISH
        else:
            broken = window < block['bottom']
            new_direction = BEARISH

        if not broken.any():
            continue

        brk_idx = start + int(np.argmax(broken))
        top, bottom = block['top'], block['bottom']

        breakers.append({
            'type': 'BREAKER',
            'direction': new_direction,
            'top': top,
            'bottom': bottom,
            'index': block['index'],
            'confirmed_at': brk_idx,
            'time': block['time'],
            'origin_index': block['index'],
            **_zone_geometry(new_direction, top, bottom),
        })

    return breakers


def find_mitigation_blocks(df, structure_obj, max_lookback=20):
    """
    Mitigation Block (§5.3) — строится на failure swing (SMS, §2.5).

    Отличие от брейкера: снятия ликвидности НЕ было. Цена не смогла обновить
    экстремум (сформировала LH в бычьем движении или HL в медвежьем), после
    чего импульсно пробила противоположный экстремум.

    Бычий MB — самая высокая медвежья свеча импульсно пробитого максимума;
    после пробоя становится зоной поддержки, потому что запертые в убытке
    шорты закрываются покупкой и дополнительно толкают цену вверх.
    """
    open_ = df['open'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    times = df['timestamp'].to_numpy() if 'timestamp' in df.columns else np.arange(len(df))

    points = structure_obj['points']
    blocks = []

    for pos, point in enumerate(points):
        # Нас интересуют именно неудачные свинги: LH в росте, HL в падении
        if point['label'] not in ('LH', 'HL'):
            continue
        prior = [p['label'] for p in points[max(0, pos - 3):pos] if p['label']]
        if point['label'] == 'LH' and 'HH' not in prior:
            continue
        if point['label'] == 'HL' and 'LL' not in prior:
            continue

        direction = BEARISH if point['label'] == 'LH' else BULLISH

        # Ищем последующий импульсный пробой противоположного экстремума
        anchor = point['confirmed_at']
        found = None
        for event in structure_obj['events']:
            if event['index'] <= anchor:
                continue
            if event['direction'] == direction:
                found = event
                break
        if found is None:
            continue

        brk = found['index']
        start = max(0, brk - max_lookback)
        mb_idx = None
        for i in range(brk - 1, start - 1, -1):
            is_bear = close[i] < open_[i]
            if (direction == BULLISH and is_bear) or (direction == BEARISH and not is_bear):
                mb_idx = i
                break
        if mb_idx is None:
            continue

        top, bottom = float(high[mb_idx]), float(low[mb_idx])
        if top <= bottom:
            continue

        blocks.append({
            'type': 'MITIGATION',
            'direction': direction,
            'top': top,
            'bottom': bottom,
            'index': mb_idx,
            'confirmed_at': brk,
            'time': times[mb_idx],
            'sms_index': point['index'],
            **_zone_geometry(direction, top, bottom),
        })

    return blocks


def find_wick_zones(df, sweeps):
    """
    Тень свечи как зона интереса (§5.4).

    Методичка: логика идентична Order Block, отличие исключительно визуальное —
    вместо тела свечи работает область тени. Берём тени свечей, которыми было
    снято накопление ликвидности.

    Первая зона реакции — начало тени, вторая — 50% тени.
    """
    open_ = df['open'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    times = df['timestamp'].to_numpy() if 'timestamp' in df.columns else np.arange(len(df))

    zones = []
    for sweep in sweeps:
        i = sweep['index']
        body_top = max(open_[i], close[i])
        body_low = min(open_[i], close[i])

        if sweep['side'] == 'BSL':
            # Снята ликвидность сверху — работает верхняя тень, зона сопротивления
            top, bottom, direction = float(high[i]), float(body_top), BEARISH
        else:
            top, bottom, direction = float(body_low), float(low[i]), BULLISH

        if top <= bottom:
            continue

        zones.append({
            'type': 'WICK',
            'direction': direction,
            'top': top,
            'bottom': bottom,
            'index': i,
            'confirmed_at': sweep['reclaimed_at'],
            'time': times[i],
            'sweep_source': sweep['source'],
            **_zone_geometry(direction, top, bottom),
        })

    return zones


def collect_pois(df, structure_obj, sweeps=None):
    """
    Собирает все типы зон интереса в один список.

    Порядок не гарантирован — фильтрацию и выбор лучшей зоны делает
    active_pois() и уже сам генератор сигналов.
    """
    sweeps = sweeps or []
    order_blocks = find_order_blocks(df, structure_obj)

    pois = list(order_blocks)
    pois += find_breaker_blocks(df, structure_obj, order_blocks)
    pois += find_mitigation_blocks(df, structure_obj)
    pois += find_wick_zones(df, sweeps)

    return sorted(pois, key=lambda p: p['confirmed_at'])


def active_pois(df, pois, at_index, direction=None,
                max_age=None, max_touches=None):
    """
    Зоны, актуальные на свече `at_index`.

    Отсеиваем: неподтверждённые, устаревшие, уже пробитые насквозь (зона
    инвалидирована) и перетестированные — §5: ранее протестированные POI
    повторной реакции обычно не дают.
    """
    max_age = params.POI_MAX_AGE_BARS if max_age is None else max_age
    max_touches = params.POI_MAX_TOUCHES if max_touches is None else max_touches

    close = df['close'].to_numpy(dtype=float)
    out = []

    for poi in pois:
        if poi['confirmed_at'] > at_index:
            continue
        if at_index - poi['confirmed_at'] > max_age:
            continue
        if direction and poi['direction'] != direction:
            continue

        # Зона инвалидирована, если цена закрылась за её дальней границей
        segment = close[poi['confirmed_at'] + 1:at_index + 1]
        if len(segment):
            if poi['direction'] == BULLISH and segment.min() < poi['bottom']:
                continue
            if poi['direction'] == BEARISH and segment.max() > poi['top']:
                continue

        touches = count_touches(df, poi, poi['confirmed_at'], at_index)
        if touches > max_touches:
            continue

        out.append({**poi, 'touches': touches})

    return out


def score_poi(poi, extras=None):
    """
    Оценка качества зоны 0..1 для выбора лучшей, когда их несколько.

    Базой служит приоритет типа зоны (POI_TYPE_PRIORITY), к нему добавляются
    бонусы за силу подтверждающего импульса и свежесть.
    """
    extras = extras or {}
    score = params.POI_TYPE_PRIORITY.get(poi['type'], 0.5)

    impulse = poi.get('impulse_pct') or 0.0
    score += min(impulse / 0.05, 1.0) * 0.25       # импульс 5%+ даёт максимум

    if poi.get('touches', 0) == 0:
        score += 0.15

    if extras.get('fvg_present'):
        score += 0.15
    if extras.get('liquidity_swept'):
        score += 0.20

    return score
