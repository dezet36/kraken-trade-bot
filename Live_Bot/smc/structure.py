"""
Структура рынка (§2.2-2.5 методички).

Разметка HH/HL/LH/LL, события слома структуры (BOS) и смены характера
движения (CHoCH — в методичке «слом тренда»), а также failure swing / SMS.

Терминология методички:
    BOS   — пробой значимого структурного уровня ПО тренду (продолжение);
    CHoCH — пробой подтверждённого HL (в бычьем) или LH (в медвежьем), то есть
            слом тренда. Методичка §2.2: «обновление подтверждённого HL
            является сломом бычьего тренда»;
    mBOS  — слом внутренней (суб-) структуры, считается по минорным свингам;
    SMS   — failure swing: тренд не смог обновить экстремум (§2.5), основа
            для Mitigation Block.

Тренд считается ПОДТВЕРЖДЁННЫМ только после трёх структурных элементов
подряд: HH-HL-HH для восходящего, LL-LH-LL для нисходящего (§2.2).
"""

from bisect import bisect_right

import numpy as np

from . import params, swings as swings_mod

BULLISH = 'BULLISH'
BEARISH = 'BEARISH'
NEUTRAL = 'NEUTRAL'


def label_swings(merged):
    """
    Размечает хронологическую последовательность свингов на HH/HL/LH/LL.

    Каждый максимум сравнивается с предыдущим максимумом, каждый минимум —
    с предыдущим минимумом. Первый свинг каждого типа разметить не с чем,
    он получает label=None.
    """
    labelled = []
    prev_high = None
    prev_low = None

    for swing in merged:
        point = dict(swing)
        if swing['kind'] == 'high':
            if prev_high is None:
                point['label'] = None
            else:
                point['label'] = 'HH' if swing['price'] > prev_high['price'] else 'LH'
            prev_high = swing
        else:
            if prev_low is None:
                point['label'] = None
            else:
                point['label'] = 'HL' if swing['price'] > prev_low['price'] else 'LL'
            prev_low = swing
        labelled.append(point)

    return labelled


def build_structure(df, n=None, break_on_close=None, tier='swing'):
    """
    Строит полную структуру по DataFrame свечей.

    n            — размер фрактала (None -> структурный из params)
    break_on_close — пробой засчитывается по закрытию тела (§2.3), не по тени
    tier         — 'swing' (структурная) или 'minor' (внутренняя, даёт mBOS)

    Возвращает словарь:
        points  — размеченные свинги (HH/HL/LH/LL)
        events  — хронология сломов: {'index','time','type','direction','level'}
        trend   — состояние тренда на последней свече
        confirmed_trend — тренд, подтверждённый тремя элементами (§2.2)
        _event_index — служебный список индексов для быстрых запросов state_at
    """
    if n is None:
        n = params.SWING_N_MINOR if tier == 'minor' else params.SWING_N_STRUCT
    if break_on_close is None:
        break_on_close = params.BREAK_ON_CLOSE

    highs, lows = swings_mod.find_swings(df, n=n)
    merged = swings_mod.merge_swings(highs, lows)
    points = label_swings(merged)

    bos_tag = 'MBOS' if tier == 'minor' else 'BOS'
    choch_tag = 'MCHOCH' if tier == 'minor' else 'CHOCH'

    size = len(df)
    close = df['close'].to_numpy(dtype=float)
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    times = df['timestamp'].to_numpy() if 'timestamp' in df.columns else np.arange(size)

    # Свинги, сгруппированные по свече, на которой они становятся видимы
    by_confirm = {}
    for point in points:
        by_confirm.setdefault(point['confirmed_at'], []).append(point)

    events = []
    trend = NEUTRAL
    ref_high = None   # последний подтверждённый непробитый максимум
    ref_low = None    # последний подтверждённый непробитый минимум

    for i in range(size):
        # 1) Публикуем свинги, подтверждённые именно на этой свече
        for point in by_confirm.get(i, ()):
            if point['kind'] == 'high':
                ref_high = point
            else:
                ref_low = point

        # 2) Проверяем пробой. Тело или тень — по настройке.
        up_price = close[i] if break_on_close else high[i]
        down_price = close[i] if break_on_close else low[i]

        if ref_high is not None and up_price > ref_high['price']:
            # Пробой вверх: по тренду — BOS, против — слом тренда (CHoCH)
            kind = bos_tag if trend == BULLISH else choch_tag
            events.append({
                'index': i,
                'time': times[i],
                'type': kind,
                'direction': BULLISH,
                'level': ref_high['price'],
                'broken_index': ref_high['index'],
            })
            trend = BULLISH
            ref_high = None   # уровень отработан, ждём следующий свинг

        elif ref_low is not None and down_price < ref_low['price']:
            kind = bos_tag if trend == BEARISH else choch_tag
            events.append({
                'index': i,
                'time': times[i],
                'type': kind,
                'direction': BEARISH,
                'level': ref_low['price'],
                'broken_index': ref_low['index'],
            })
            trend = BEARISH
            ref_low = None

    # Хронология ПОДТВЕРЖДЁННОГО тренда (§2.2: три структурных элемента
    # подряд). Нужна отдельно от событий слома: направление последнего слома
    # меняется на каждом проколе и в боковике скачет туда-сюда, а
    # подтверждённый тренд требует настоящей последовательности HH-HL-HH.
    confirmed_timeline = []
    seen_labels = []
    for point in points:
        if point['label']:
            seen_labels.append(point['label'])
        state = _confirmed_trend_from_labels(seen_labels)
        if not confirmed_timeline or confirmed_timeline[-1][1] != state:
            confirmed_timeline.append((point['confirmed_at'], state))

    return {
        'tier': tier,
        'points': points,
        'events': events,
        'trend': trend,
        'confirmed_trend': _confirmed_trend(points),
        '_event_index': [e['index'] for e in events],
        '_confirmed_timeline': confirmed_timeline,
        '_confirmed_index': [c[0] for c in confirmed_timeline],
    }


def _confirmed_trend_from_labels(labels, legs=None):
    """
    Тренд подтверждён тремя структурными элементами подряд (§2.2):
    HH-HL-HH для восходящего, LL-LH-LL для нисходящего.
    """
    legs = params.TREND_CONFIRM_LEGS if legs is None else legs
    if len(labels) < legs:
        return NEUTRAL

    tail = labels[-legs:]
    if all(lbl in ('HH', 'HL') for lbl in tail) and 'HH' in tail:
        return BULLISH
    if all(lbl in ('LL', 'LH') for lbl in tail) and 'LL' in tail:
        return BEARISH
    return NEUTRAL


def _confirmed_trend(points, legs=None):
    """Подтверждённый тренд по всей размеченной последовательности."""
    return _confirmed_trend_from_labels(
        [p['label'] for p in points if p['label']], legs)


def confirmed_trend_at(structure, index):
    """
    Подтверждённый тренд на свече `index` — без пересчёта истории.

    Отличие от state_at(): тот отдаёт направление ПОСЛЕДНЕГО слома, которое в
    боковике скачет на каждом проколе уровня. Здесь требуется настоящая
    последовательность HH-HL-HH, поэтому в пиле состояние честно остаётся
    NEUTRAL — и стратегия просто не торгует.

    Прогон на 2022-2023 показал, зачем это нужно: SMC зарабатывает в
    направленном рынке (обвал 2022 H1 дал PF 1.358, бычий 2025-26 — 1.664),
    но теряет на дне и в боковике (0.854 и 0.969). Это трендовая стратегия,
    и ей нужен фильтр режима, а не очередной фильтр сетапов.
    """
    timeline = structure.get('_confirmed_index')
    if not timeline:
        return NEUTRAL
    pos = bisect_right(timeline, index)
    if pos == 0:
        return NEUTRAL
    return structure['_confirmed_timeline'][pos - 1][1]


def state_at(structure, index):
    """
    Состояние структуры на свече `index` — без пересчёта всей истории.

    Нужно бэктесту: структура строится один раз на пару, а решение принимается
    на каждой свече. Тренд меняется только в моменты событий, поэтому
    достаточно найти последнее событие с index <= запрошенного.
    """
    pos = bisect_right(structure['_event_index'], index)
    if pos == 0:
        return {'trend': NEUTRAL, 'last_event': None}

    last_event = structure['events'][pos - 1]
    return {'trend': last_event['direction'], 'last_event': last_event}


def visible_points(structure, index):
    """Размеченные свинги, подтверждённые к свече `index`."""
    return [p for p in structure['points'] if p['confirmed_at'] <= index]


def last_leg(structure, index=None):
    """
    Последняя импульсная нога (от свинга к противоположному свингу).

    Нужна для растяжки сетки Фибоначчи (§10.1): для восходящего тренда сетка
    тянется от HL к HH, для нисходящего — от LH к LL.

    Возвращает {'direction','start','end','size'} или None.
    """
    pool = structure['points'] if index is None else visible_points(structure, index)
    if len(pool) < 2:
        return None

    end = pool[-1]
    # Ищем ближайший противоположный свинг слева
    start = None
    for point in reversed(pool[:-1]):
        if point['kind'] != end['kind']:
            start = point
            break
    if start is None:
        return None

    size = abs(end['price'] - start['price'])
    if size <= 0:
        return None

    return {
        'direction': BULLISH if end['kind'] == 'high' else BEARISH,
        'start': start,
        'end': end,
        'size': size,
    }


def invalidation_level(structure, index=None, direction=None):
    """
    Цена, за которой структура МЕНЯЕТ ХАРАКТЕР, — та самая точка CHoCH.

    Для бычьей структуры это последний подтверждённый HL: пока цена выше,
    последовательность «выше низы, выше верхи» цела; закрытие ниже — и это
    уже не коррекция, а смена характера движения. Для медвежьей —
    зеркально, последний LH.

    ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ. Место стопа — это не «край ближайшей зоны», а
    цена, после которой идея сделки перестаёт существовать. Аудит 22.09.2026
    показал, чем оборачивается подмена: у 10 планов ИИ из 14 вход стоял ПО ТУ
    СТОРОНУ этого уровня — то есть покупка назначалась туда, куда цена может
    прийти, только сломав тренд, ради которого покупали. Стоп при этом
    ставился за ближний край имбаланса — в треть дневного размаха.

    Уровень считается по подтверждённым свингам: неподтверждённый экстремум
    ещё может перерисоваться, и стоп за ним пришлось бы двигать.

    direction — BULLISH или BEARISH; None = взять текущий тренд структуры.
    Возвращает {'price', 'label', 'index', 'direction', 'broken'} или None.
    'broken' = True, если последнее событие структуры уже CHoCH: характер
    сменился, и прежняя идея держаться не на чем.
    """
    if direction is None:
        direction = state_at(structure, index if index is not None
                             else len(structure.get('_event_index') or []) - 1)['trend']
    if direction not in (BULLISH, BEARISH):
        return None
    want = 'HL' if direction == BULLISH else 'LH'
    pool = structure['points'] if index is None else visible_points(structure, index)
    for point in reversed(pool):
        if point.get('label') == want:
            state = state_at(structure, index if index is not None else point['index'])
            last = (state or {}).get('last_event') or {}
            return {
                'price': float(point['price']),
                'label': want,
                'index': int(point['index']),
                'direction': direction,
                'broken': str(last.get('type', '')).endswith('CHOCH'),
            }
    return None
