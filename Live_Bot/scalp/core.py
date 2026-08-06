"""
Пробой уровня после прижатия: поиск сетапа.

ОДНА реализация на замер и на бой. У стратегии уровней это правило когда-то
нарушили — исследовательская копия разошлась с боевой, и месяц наблюдений
измерял не то, что торговал. Здесь функции чистые: на вход массивы свечей и
индекс, на выход описание сетапа. Живой бот зовёт их на последней закрытой
свече, замер — на каждой по очереди, и код у обоих один.

ЧТО ИМЕННО ИЩЕТСЯ

    1. Уровень — цена, от которой уже отбивались не меньше двух раз.
    2. Прижатие — цена подошла к уровню и замерла: диапазон последних баров
       сжался, и большая их часть держалась вплотную к уровню.
    3. Пробой — свеча ЗАКРЫЛАСЬ за уровнем на заметное расстояние, и объём
       на ней выше среднего.

Третий пункт — главное отличие от прежнего замера, который потерял деньги.
Там пробоем считался уход цены за границу канала, то есть в том числе тенью.
Тень за уровнем — это чаще снятие ликвидности, чем пробой: за экстремумом
стоят стопы, их собирают, и цена возвращается. Здесь нужно закрытие.

ЧЕГО ЗДЕСЬ НЕТ. Заглядывания вперёд. Уровни, ATR и объём считаются по барам
СТРОГО ДО сигнального включительно; ничего из будущего в расчёт не попадает.
Проверить это легко: любая функция принимает индекс `at` и не смотрит дальше.
"""

import numpy as np

from . import params


LONG, SHORT = 'LONG', 'SHORT'


def atr(high, low, close, at, period=14):
    """Средний истинный диапазон по барам до `at` включительно."""
    start = max(1, at - period + 1)
    if at < 1:
        return 0.0
    prev_close = close[start - 1:at]
    highs = high[start:at + 1]
    lows = low[start:at + 1]
    if len(highs) == 0:
        return 0.0
    n = min(len(highs), len(lows), len(prev_close) + 1)
    highs, lows = highs[-n:], lows[-n:]
    prev_close = close[start - 1:at][-n:] if n <= len(prev_close) else close[start - 1:at]
    if len(prev_close) < n:
        prev_close = np.concatenate([prev_close, close[at - 1:at]])[:n]
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    return float(np.mean(tr)) if len(tr) else 0.0


def find_levels(high, low, at):
    """
    Горизонтальные уровни по экстремумам баров до `at`.

    Экстремумы, стоящие рядом по цене, сливаются в один уровень: рынок не
    помнит копеек, он помнит область. Число касаний — это и есть вес уровня.
    """
    n = params.PIVOT_N
    start = max(n, at - params.MAX_AGE_BARS)
    points = []
    for i in range(start, at - n + 1):
        window_h = high[i - n:i + n + 1]
        window_l = low[i - n:i + n + 1]
        if len(window_h) < 2 * n + 1:
            continue
        if high[i] >= window_h.max():
            points.append((float(high[i]), i))
        if low[i] <= window_l.min():
            points.append((float(low[i]), i))

    # Индекс касания едет вместе с ценой. Отбор от него не зависит — он нужен,
    # чтобы посчитать ВОЗРАСТ уровня: свежий уровень из двух касаний за час и
    # уровень, который держит цену третьи сутки, — разные вещи, а «касаний 2»
    # у них одинаковое.
    levels = []
    for price, index in sorted(points):
        if levels and abs(price - levels[-1]['sum'] / levels[-1]['touches']) \
                <= price * params.TOLERANCE_PCT / 100:
            levels[-1]['sum'] += price
            levels[-1]['touches'] += 1
            levels[-1]['first'] = min(levels[-1]['first'], index)
        else:
            levels.append({'sum': price, 'touches': 1, 'first': index})
    return [{'price': lv['sum'] / lv['touches'], 'touches': lv['touches'],
             'age': at - lv['first']}
            for lv in levels if lv['touches'] >= params.MIN_TOUCHES]


def squeeze(high, low, close, at, level, atr_now):
    """
    Было ли прижатие к уровню в окне, заканчивающемся на `at`.

    Два условия сразу, и оба нужны. Узкий диапазон без привязки к уровню —
    это просто затишье где угодно. Близость к уровню без сжатия — это проход
    мимо. Приём работает, когда цена подошла И замерла.

    Возвращает (низ окна, верх окна) либо None.
    """
    window = params.SQUEEZE_BARS
    if at < window or atr_now <= 0:
        return None
    lo = float(np.min(low[at - window + 1:at + 1]))
    hi = float(np.max(high[at - window + 1:at + 1]))
    if hi - lo > params.SQUEEZE_ATR * atr_now:
        return None

    near = np.abs(close[at - window + 1:at + 1] - level) <= params.NEAR_ATR * atr_now
    if int(near.sum()) < params.NEAR_MIN_BARS:
        return None
    return lo, hi


def volume_ok(volume, at):
    """Объём на сигнальной свече выше среднего — за пробоем кто-то стоит."""
    start = max(0, at - params.VOLUME_WINDOW)
    if at <= start:
        return False, 0.0
    average = float(np.mean(volume[start:at]))
    if average <= 0:
        return False, 0.0
    ratio = float(volume[at]) / average
    return ratio >= params.VOLUME_RATIO, ratio


def squeeze_traits(high, low, volume, at, side, atr_now):
    """
    Два признака прижатия, которых в отборе НЕТ, — они только записываются.

    ВЫСЫХАНИЕ ОБЪЁМА. Внешние источники дают самое сильное число всей темы:
    падение объёма к моменту пробоя с последующим всплеском даёт около 65%
    попаданий, а пробой на объёме ниже среднего — 48%, то есть хуже монетки.
    Мы до сих пор смотрели только на всплеск НА пробойной свече. Сохнущий
    объём внутри самого прижатия — другая величина и другой смысл: продавец
    кончился, а не отошёл. Возвращается отношение «объём в прижатии к объёму
    до него»: меньше единицы — сохнет.

    НАПРАВЛЕННОСТЬ. Прижатие бывает симметричным — просто узкий коридор, — а
    бывает односторонним: растущие минимумы упираются в плоское сопротивление
    (или падающие максимумы в плоскую поддержку). Это разные вещи. Первое
    означает затишье, второе — поглощение: одна сторона выедает лимитники
    другой. Наш отбор до сих пор мерил только ширину коридора.

    Возвращается наклон в долях ATR за бар и признак совпадения наклона с
    направлением пробоя. Наклон считается по минимумам для лонга и по
    максимумам для шорта: поджимают именно ту границу, к которой давят.
    """
    window = params.SQUEEZE_BARS
    lo_idx = at - window + 1
    if lo_idx < 1 or atr_now <= 0:
        return {}

    inside = volume[lo_idx:at + 1]
    before_start = max(0, lo_idx - window * 2)
    before = volume[before_start:lo_idx]
    dry = (float(np.mean(inside)) / float(np.mean(before))
           if len(before) and float(np.mean(before)) > 0 else float('nan'))

    edge = low[lo_idx:at + 1] if side == LONG else high[lo_idx:at + 1]
    steps = np.arange(len(edge), dtype=float)
    # Наклон методом наименьших квадратов, а не «конец минус начало»: одна
    # случайная тень на краю окна перевернула бы знак у второго способа.
    slope = float(np.polyfit(steps, edge, 1)[0]) / atr_now if len(edge) > 2 else 0.0
    directed = slope > 0 if side == LONG else slope < 0

    return {'vol_dry': dry, 'slope_atr': slope, 'directed': bool(directed),
            'vol_squeeze': float(np.mean(inside))}


def find_setup(high, low, close, volume, at):
    """
    Готовый сетап на баре `at` либо None.

    Порядок проверок — от дешёвых к дорогим: сначала ATR и уровни, потом
    прижатие, потом объём. На 115 тысячах баров это заметная разница.
    """
    if at < max(params.SQUEEZE_BARS, params.VOLUME_WINDOW) + params.PIVOT_N + 2:
        return None
    atr_now = atr(high, low, close, at)
    if atr_now <= 0:
        return None

    price = float(close[at])
    levels = find_levels(high, low, at)
    if not levels:
        return None
    levels.sort(key=lambda lv: abs(lv['price'] - price))

    for level in levels[:params.NEAREST_LEVELS]:
        value = level['price']
        moved = price - value
        if abs(moved) < params.BREAK_ATR * atr_now:
            continue                      # закрытие не ушло за уровень
        side = LONG if moved > 0 else SHORT

        # До пробоя цена должна была быть С ДРУГОЙ стороны уровня, иначе это
        # не пробой, а продолжение уже начавшегося движения.
        before = float(close[at - 1])
        if side == LONG and before > value:
            continue
        if side == SHORT and before < value:
            continue

        box = squeeze(high, low, close, at - 1, value, atr_now)
        if box is None:
            continue
        box_low, box_high = box

        ok, ratio = volume_ok(volume, at)
        if not ok:
            continue

        traits = squeeze_traits(high, low, volume, at - 1, side, atr_now)
        # Всплеск считается ОТ ОБЪЁМА В ПРИЖАТИИ, а не от двадцатибарного
        # среднего: именно эту величину и называют источники, и она честнее —
        # среднее по двадцати барам включает сам подход к уровню.
        squeeze_vol = traits.get('vol_squeeze') or 0.0
        surge = float(volume[at]) / squeeze_vol if squeeze_vol > 0 else float('nan')

        return {
            'direction': side,
            'level': value,
            'touches': level['touches'],
            'age': level.get('age', 0),
            'box_low': box_low,
            'box_high': box_high,
            'box_height': box_high - box_low,
            'atr': atr_now,
            'volume_ratio': ratio,
            'vol_dry': traits.get('vol_dry', float('nan')),
            'vol_surge': surge,
            'slope_atr': traits.get('slope_atr', 0.0),
            'directed': traits.get('directed', False),
            'close': price,
        }
    return None


def build_trade(setup, entry_price, stop_mode=None, target_mult=None,
                min_rr=None):
    """
    Стоп, цель и отношение к риску для найденного сетапа.

    ДВЕ ШКОЛЫ ПОСТАНОВКИ СТОПА, и обе меряются:

        box   — за противоположную границу прижатия. Пробой не состоялся,
                если цена вернулась внутрь диапазона. Стоп широкий, зато
                выбить его случайной тенью трудно.
        level — сразу за пробитый уровень, на 0.25-0.5 ATR. Стоп узкий,
                позиция крупная, но и выносит чаще.

    ВАЖНОЕ, ЧТО ВСКРЫЛОСЬ ПРИ ПЕРВОМ ЖЕ ПРОГОНЕ. Стоп за всё прижатие в паре
    с целью в одну его высоту даёт RR около единицы ПО ПОСТРОЕНИЮ: вход стоит
    у верхней границы, риск равен высоте коробки плюс буфер, прибыль — той же
    высоте. Из 168 сетапов геометрию проходил один. Это не настройка, а
    арифметика: либо цель в две высоты, либо стоп за уровень, либо и то и
    другое. Поэтому оба рычага вынесены в параметры и сравниваются замером,
    а не выбираются на глаз.

    Пол по расстоянию до стопа здесь не про шум, а про издержки: круг стоит
    0.21% от объёма, и при стопе 0.2% это больше рубля с каждого рубля риска.

    Возвращает None, если геометрия не окупает риск.
    """
    side = setup['direction']
    pad = params.STOP_PAD_ATR * setup['atr']
    stop_mode = stop_mode or params.STOP_MODE
    target_mult = params.TARGET_RANGE_MULT if target_mult is None else target_mult
    min_rr = params.MIN_TARGET_R if min_rr is None else min_rr

    if side == LONG:
        base = setup['box_low'] if stop_mode == 'box' else setup['level']
        stop = min(base - pad, entry_price * (1 - params.MIN_STOP_PCT / 100))
        target = entry_price + setup['box_height'] * target_mult
    else:
        base = setup['box_high'] if stop_mode == 'box' else setup['level']
        stop = max(base + pad, entry_price * (1 + params.MIN_STOP_PCT / 100))
        target = entry_price - setup['box_height'] * target_mult

    distance = abs(entry_price - stop)
    if distance <= 0:
        return None
    rr = abs(target - entry_price) / distance
    if rr < min_rr:
        # Цель оказалась ближе стопа: сетап есть, а сделки нет. Растягивать
        # цель до нужного RR нельзя — это уже не measured move, а желаемое.
        return None

    return {'entry': entry_price, 'stop': stop, 'target': target, 'rr': rr,
            'stop_pct': distance / entry_price * 100}
