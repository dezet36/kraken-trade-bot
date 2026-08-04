"""
Ликвидность (§3 методички).

Ликвидность — открытый интерес покупателей и продавцов, определяемый
стоп-лоссами. Умный капитал ищет её, чтобы заполнить свои ордера: заявки на
покупку заполняются НИЖЕ старых минимумов, на продажу — ВЫШЕ старых максимумов.

    BSL (Buy Stops Liquidity)  — стопы шортистов выше максимумов;
    SSL (Sell Stops Liquidity) — стопы лонгистов ниже минимумов.

Значимые уровни (§3.3), от старшего к младшему:
    PMH/PML — максимум/минимум предыдущего месяца
    PWH/PWL — предыдущей недели
    PDH/PDL — предыдущего дня
    HOD/LOD — текущего дня
    Old High/Low — любой старый свинг
    EQH/EQL — равные вершины/низы (искусственный магнит для цены)

Ключевая механика для сигнала — СНЯТИЕ ликвидности (sweep): цена прокалывает
уровень, собирает стопы и возвращается обратно. §3.3 отдельно предупреждает:
незначительное обновление свинга снятием не считается — цена к нему вернётся.
"""

import numpy as np
import pandas as pd

from . import params, swings as swings_mod

BSL = 'BSL'
SSL = 'SSL'


def build_reference_levels(df, day_open_hour=None):
    """
    Уровни предыдущих периодов (§3.3) для каждой свечи.

    Возвращает DataFrame, выровненный по индексу df, с колонками
    pdh/pdl/pwh/pwl/pmh/pml — значения ПРЕДЫДУЩЕГО завершённого периода,
    то есть известные на текущей свече (сдвиг на 1 период обязателен,
    иначе бот увидит максимум ещё не закрытого дня).

    day_open_hour — час UTC, с которого начинается торговый день. Методичка
    §11.1: открытие дня в 02:00 UTC+2, то есть 00:00 UTC.
    """
    day_open_hour = params.DAY_OPEN_HOUR_UTC if day_open_hour is None else day_open_hour

    if 'timestamp' not in df.columns:
        raise ValueError('build_reference_levels требует колонку timestamp')

    ts = pd.to_datetime(df['timestamp'], utc=True)
    shifted = ts - pd.Timedelta(hours=day_open_hour)

    frame = pd.DataFrame({
        'high': df['high'].to_numpy(dtype=float),
        'low': df['low'].to_numpy(dtype=float),
    }, index=shifted)

    out = pd.DataFrame(index=df.index)
    for label, rule in (('d', 'D'), ('w', 'W'), ('m', 'MS')):
        grouped = frame.resample(rule)
        period_high = grouped['high'].max().shift(1)
        period_low = grouped['low'].min().shift(1)
        # reindex по каждой свече: значение прошлого завершённого периода
        out[f'p{label}h'] = period_high.reindex(shifted, method='ffill').to_numpy()
        out[f'p{label}l'] = period_low.reindex(shifted, method='ffill').to_numpy()

    return out


def find_equal_levels(points, kind, tolerance_pct=None,
                      min_apart=None, max_apart=None):
    """
    Равные вершины (EQH) или равные низы (EQL) — §3.3.

    Методичка: двойная/тройная вершина, искусственно формируемая умными
    деньгами как магнит для цены. Ищем пары свингов одного типа, чьи цены
    отличаются меньше допуска и которые разнесены во времени.

    points — размеченные свинги (из structure) или сырые свинги
    kind    — 'high' для EQH, 'low' для EQL
    """
    tolerance_pct = params.EQ_TOLERANCE_PCT if tolerance_pct is None else tolerance_pct
    min_apart = params.EQ_MIN_BARS_APART if min_apart is None else min_apart
    max_apart = params.EQ_MAX_BARS_APART if max_apart is None else max_apart

    pool = [p for p in points if p['kind'] == kind]
    clusters = []

    for i, first in enumerate(pool):
        members = [first]
        for second in pool[i + 1:]:
            gap = second['index'] - members[-1]['index']
            if gap < min_apart:
                continue
            if second['index'] - first['index'] > max_apart:
                break
            if abs(second['price'] - first['price']) / first['price'] <= tolerance_pct:
                members.append(second)
        if len(members) >= 2:
            clusters.append({
                'type': 'EQH' if kind == 'high' else 'EQL',
                # Уровень ликвидности лежит за самым экстремальным из равных
                'price': max(m['price'] for m in members) if kind == 'high'
                         else min(m['price'] for m in members),
                'index': members[-1]['index'],
                'confirmed_at': max(m['confirmed_at'] for m in members),
                'members': [m['index'] for m in members],
                'count': len(members),
            })

    # Отбрасываем кластеры, целиком поглощённые более крупными
    unique = []
    seen = set()
    for cluster in sorted(clusters, key=lambda c: -c['count']):
        key = frozenset(cluster['members'])
        if any(key <= s for s in seen):
            continue
        seen.add(key)
        unique.append(cluster)

    return sorted(unique, key=lambda c: c['index'])


def find_liquidity_pools(df, structure_obj, include_reference=True):
    """
    Собирает все пулы ликвидности: свинговые уровни, EQH/EQL и уровни
    предыдущих периодов.

    Возвращает список словарей:
        {'side': 'BSL'|'SSL', 'price', 'index', 'confirmed_at', 'source', 'weight'}

    weight отражает значимость уровня по §3.3 (месяц > неделя > день > свинг).
    """
    pools = []
    points = structure_obj['points']

    for point in points:
        pools.append({
            'side': BSL if point['kind'] == 'high' else SSL,
            'price': point['price'],
            'index': point['index'],
            'confirmed_at': point['confirmed_at'],
            'source': 'SWING',
            'weight': 0.5,
        })

    for cluster in find_equal_levels(points, 'high'):
        pools.append({
            'side': BSL, 'price': cluster['price'], 'index': cluster['index'],
            'confirmed_at': cluster['confirmed_at'], 'source': 'EQH', 'weight': 0.9,
        })
    for cluster in find_equal_levels(points, 'low'):
        pools.append({
            'side': SSL, 'price': cluster['price'], 'index': cluster['index'],
            'confirmed_at': cluster['confirmed_at'], 'source': 'EQL', 'weight': 0.9,
        })

    if include_reference and 'timestamp' in df.columns:
        levels = build_reference_levels(df)
        # Уровень периода актуален с той свечи, где он впервые известен;
        # берём точки смены значения, чтобы не плодить дубликаты на каждой свече.
        for col, side, source, weight in (
            ('pdh', BSL, 'PDH', 0.7), ('pdl', SSL, 'PDL', 0.7),
            ('pwh', BSL, 'PWH', 0.85), ('pwl', SSL, 'PWL', 0.85),
            ('pmh', BSL, 'PMH', 1.0), ('pml', SSL, 'PML', 1.0),
        ):
            series = levels[col]
            changed = series.ne(series.shift(1)) & series.notna()
            for idx in np.flatnonzero(changed.to_numpy()):
                pools.append({
                    'side': side,
                    'price': float(series.iloc[idx]),
                    'index': int(idx),
                    'confirmed_at': int(idx),
                    'source': source,
                    'weight': weight,
                })

    return sorted(pools, key=lambda p: p['index'])


def find_sweeps(df, pools, min_penetration=None, reclaim_bars=None):
    """
    Снятие ликвидности (§3.3): цена прокалывает уровень, собирает стопы и
    возвращается обратно за него.

    Отличие снятия от обычного пробоя — возврат. Если цена ушла за уровень и
    не вернулась в течение reclaim_bars свечей, это пробой с продолжением, а
    не снятие, и такой уровень как сетап не годится.

    Возвращает список:
        {'index', 'pool', 'side', 'penetration_pct', 'reclaimed_at', 'direction'}

    direction — куда ожидается разворот ПОСЛЕ снятия: снятие BSL (сверху)
    даёт медвежий сетап, снятие SSL — бычий.
    """
    min_penetration = (params.SWEEP_MIN_PENETRATION_PCT if min_penetration is None
                       else min_penetration)
    reclaim_bars = params.SWEEP_RECLAIM_BARS if reclaim_bars is None else reclaim_bars

    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    size = len(df)
    sweeps = []

    for pool in pools:
        start = pool['confirmed_at'] + 1
        if start >= size:
            continue
        level = pool['price']

        if pool['side'] == BSL:
            threshold = level * (1 + min_penetration)
            beyond = high[start:] > threshold
        else:
            threshold = level * (1 - min_penetration)
            beyond = low[start:] < threshold

        if not beyond.any():
            continue

        pierce = start + int(np.argmax(beyond))

        # Возврат за уровень в пределах окна — иначе это пробой, не снятие
        window_end = min(pierce + reclaim_bars + 1, size)
        window = close[pierce:window_end]
        if pool['side'] == BSL:
            back = window < level
        else:
            back = window > level

        if not back.any():
            continue

        reclaimed_at = pierce + int(np.argmax(back))
        extreme = high[pierce] if pool['side'] == BSL else low[pierce]

        sweeps.append({
            'index': pierce,
            'reclaimed_at': reclaimed_at,
            'pool': pool,
            'side': pool['side'],
            'source': pool['source'],
            'weight': pool['weight'],
            'level': level,
            'extreme': float(extreme),
            'penetration_pct': abs(extreme - level) / level,
            # После снятия BSL ждём разворот вниз, после SSL — вверх
            'direction': 'BEARISH' if pool['side'] == BSL else 'BULLISH',
        })

    return sorted(sweeps, key=lambda s: s['index'])


def recent_sweep(sweeps, index, direction=None, fresh_bars=None):
    """
    Последнее свежее снятие ликвидности к свече `index`.

    Используется как фактор confluence: §5 — «формирование ликвидности перед
    POI ускоряет и повышает вероятность разворота после её теста».
    """
    fresh_bars = params.SWEEP_FRESH_BARS if fresh_bars is None else fresh_bars

    best = None
    for sweep in sweeps:
        # Снятие считается состоявшимся только после возврата цены
        if sweep['reclaimed_at'] > index:
            continue
        if index - sweep['index'] > fresh_bars:
            continue
        if direction and sweep['direction'] != direction:
            continue
        if best is None or sweep['index'] > best['index']:
            best = sweep
    return best


def untapped_pools(pools, sweeps, index, side=None):
    """
    Пулы, которые ещё НЕ сняты к свече `index` — это цели для тейк-профита
    (§14.2: «тейк-профиты располагайте на очевидных пулах ликвидности»).
    """
    swept = {(s['pool']['source'], round(s['level'], 10))
             for s in sweeps if s['index'] <= index}

    out = []
    for pool in pools:
        if pool['confirmed_at'] > index:
            continue
        if side and pool['side'] != side:
            continue
        if (pool['source'], round(pool['price'], 10)) in swept:
            continue
        out.append(pool)
    return out
