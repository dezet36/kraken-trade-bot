"""
Ложный пробой границы коридора: поиск сетапа.

ОДНА реализация на замер и на бой — правило проекта, нарушенное однажды у
стратегии уровней и стоившее месяца недостоверных наблюдений.

ЧТО ИЩЕТСЯ
    1. Коридор — тем же кодом, что и сетка (grid.core.find_range). Он уже
       проверен: находит 13% баров, пороги взяты из распределения.
    2. Прокол его границы не меньше чем на PIERCE_ATR.
    3. ЗАКРЫТИЕ обратно внутрь коридора в пределах RECLAIM_BARS баров.
    4. Вход против прокола, цель — противоположный край.

ПОЧЕМУ ИМЕННО ЗАКРЫТИЕ, А НЕ КАСАНИЕ. Тень за границей — это и есть вынос
стопов, он ничего не сообщает о намерении. Значение имеет то, где бар
закрылся: вернулся внутрь — вынос не подтвердился как выход.

КОРИДОР ОБЯЗАН БЫТЬ ПОСТРОЕН ДО ПРОКОЛА, И ЭТО НЕ ПРИДИРКА. Первая версия
брала коридор на баре возврата — то есть по окну, КУДА ПРОКОЛ УЖЕ ВХОДИЛ.
Граница такого коридора равна минимуму окна, а значит вобрала в себя вынос:
условие «цена ушла ниже границы» становится невыполнимым по построению. На
реальных данных это дало ровно ноль сетапов на десяти тысячах баров.

Поэтому вызывающая сторона обязана передать коридор, найденный на баре
ПЕРЕД окном возврата. Заглядывания вперёд при этом не появляется: такой
коридор построен по ещё более старым данным, чем бар решения.
"""

import numpy as np

from . import sweep_params as params

LONG, SHORT = 'LONG', 'SHORT'


def _volume_ratio(volume, at, window):
    start = max(0, at - window)
    if at <= start:
        return 0.0
    average = float(np.mean(volume[start:at]))
    return float(volume[at]) / average if average > 0 else 0.0


def find_sweep(high, low, close, volume, at, box,
               pierce_atr=None, reclaim_bars=None):
    """
    Ложный пробой, завершившийся на баре `at`, либо None.

    `box` — коридор, найденный grid.core.find_range на этом же баре.
    """
    pierce_atr = params.PIERCE_ATR if pierce_atr is None else pierce_atr
    reclaim_bars = params.RECLAIM_BARS if reclaim_bars is None else reclaim_bars
    if at < reclaim_bars + 2 or box is None:
        return None

    price = float(close[at])
    need = pierce_atr * box['atr']
    # Возврат должен состояться ВНУТРЬ коридора — иначе это не вынос, а выход.
    if not (box['low'] < price < box['high']):
        return None

    start = max(0, at - reclaim_bars)
    pierce_at, extreme, side = None, None, None

    below = [k for k in range(start, at) if low[k] <= box['low'] - need]
    above = [k for k in range(start, at) if high[k] >= box['high'] + need]
    # Оба края вынесены за одно окно — это не коридор, а хаос: непонятно, чей
    # вынос отрабатывается, и любая сторона будет угадыванием.
    if below and above:
        return None
    if below:
        side = LONG
        pierce_at = below[0]
        extreme = float(min(low[k] for k in below))
        extreme = min(extreme, float(low[at]))
    elif above:
        side = SHORT
        pierce_at = above[0]
        extreme = float(max(high[k] for k in above))
        extreme = max(extreme, float(high[at]))
    else:
        return None

    return {
        'direction': side,
        'entry': price,
        'extreme': extreme,
        'pierce_at': int(pierce_at),
        'pierce_depth_atr': abs(extreme - (box['low'] if side == LONG
                                           else box['high'])) / box['atr'],
        'pierce_volume': _volume_ratio(volume, pierce_at, params.VOLUME_WINDOW),
        'reclaim_volume': _volume_ratio(volume, at, params.VOLUME_WINDOW),
        'box': box,
    }


def build_trade(setup, target_frac=None, stop_pad=None, min_rr=None,
                min_stop_pct=None):
    """
    Стоп, цель и отношение риска к прибыли. None — геометрия не годится.

    Цель на ПРОТИВОПОЛОЖНОМ крае коридора, а не на ближайшем уровне. Именно
    короткая цель убила сетку: при отношении риска к прибыли 0.38 безубыток
    требовал 72% попаданий, и три процентных пункта запаса съедала первая же
    череда убытков.
    """
    target_frac = params.TARGET_FRAC if target_frac is None else target_frac
    stop_pad = params.STOP_PAD_ATR if stop_pad is None else stop_pad
    min_rr = params.MIN_RR if min_rr is None else min_rr
    min_stop_pct = params.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct

    box = setup['box']
    entry = setup['entry']
    pad = stop_pad * box['atr']

    if setup['direction'] == LONG:
        stop = setup['extreme'] - pad
        target = entry + (box['high'] - entry) * target_frac
    else:
        stop = setup['extreme'] + pad
        target = entry - (entry - box['low']) * target_frac

    distance = abs(entry - stop)
    floor = entry * min_stop_pct / 100
    if distance < floor:
        stop = entry - floor if setup['direction'] == LONG else entry + floor
        distance = floor
    if distance <= 0:
        return None

    rr = abs(target - entry) / distance
    if rr < min_rr:
        return None
    return {'entry': entry, 'stop': stop, 'target': target, 'rr': rr,
            'stop_pct': distance / entry * 100}
