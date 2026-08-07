"""
Скальпинг по RSI и полосам Боллинджера: индикаторы и поиск сетапа.

ОДНА реализация на замер и на бой — правило проекта, нарушенное однажды у
стратегии уровней и стоившее месяца недостоверных наблюдений.

УСТРОЙСТВО. Индикаторы считаются ОДИН раз на всю серию (`indicators`), решение
принимается на отдельном баре (`evaluate`). Замер вызывает первое однократно и
второе в цикле; бот вызывает первое на последних барах и второе на последнем
индексе. Так одна и та же арифметика обслуживает оба, и разойтись им негде.

ЗАГЛЯДЫВАНИЯ ВПЕРЁД НЕТ. Все индикаторы причинные: скользящие средние и
сглаживание Уайлдера смотрят только назад. `evaluate(at)` читает массивы не
дальше индекса `at`.
"""

import numpy as np
import pandas as pd

from . import params

LONG, SHORT = 'LONG', 'SHORT'


def _wilder(values, period):
    """
    Сглаживание Уайлдера — то, на котором построены RSI и ADX.

    Копия обязательна: pandas отдаёт массив только для чтения, и вызывающая
    сторона не может пометить прогревочный участок как NaN.
    """
    smoothed = pd.Series(values).ewm(alpha=1.0 / period, adjust=False).mean()
    return np.array(smoothed.to_numpy(), dtype=float, copy=True)


def bollinger(close, period=None, mult=None):
    """Средняя линия, верхняя и нижняя полосы. NaN, пока данных не хватает."""
    period = params.BB_PERIOD if period is None else period
    mult = params.BB_MULT if mult is None else mult
    series = pd.Series(np.asarray(close, dtype=float))
    mid = series.rolling(period).mean().to_numpy()
    # Отклонение популяционное (ddof=0) — так его считает сам Боллинджер и все
    # торговые платформы. Выборочное дало бы полосы чуть шире, и пороги,
    # снятые с чужих графиков, перестали бы совпадать с нашими.
    dev = series.rolling(period).std(ddof=0).to_numpy()
    return mid, mid + mult * dev, mid - mult * dev


def rsi(close, period=None):
    """Индекс относительной силы по Уайлдеру."""
    period = params.RSI_PERIOD if period is None else period
    close = np.asarray(close, dtype=float)
    delta = np.diff(close, prepend=close[0])
    gain = _wilder(np.clip(delta, 0, None), period)
    loss = _wilder(np.clip(-delta, 0, None), period)
    out = np.full(len(close), 50.0)
    moving = loss > 0
    out[moving] = 100.0 - 100.0 / (1.0 + gain[moving] / loss[moving])
    out[(~moving) & (gain > 0)] = 100.0
    out[:period] = np.nan
    return out


def adx(high, low, close, period=None):
    """
    Индекс направленного движения. Мера СИЛЫ тренда, безразличная к его знаку.

    Нужен ровно для одного: отличить боковик, где возврат к среднему работает,
    от тренда, где цена идёт вдоль полосы и та же сделка становится убыточной.
    """
    period = params.ADX_PERIOD if period is None else period
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)

    up = np.diff(high, prepend=high[0])
    down = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev),
                                           np.abs(low - prev)))
    atr = _wilder(tr, period)
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = 100.0 * _wilder(plus_dm, period) / atr
        minus_di = 100.0 * _wilder(minus_dm, period) / atr
        total = plus_di + minus_di
        dx = 100.0 * np.abs(plus_di - minus_di) / total
    dx = np.where(np.isfinite(dx), dx, 0.0)
    out = _wilder(dx, period)
    out[:period * 2] = np.nan
    return out


def indicators(open_, high, low, close, bb_period=None, bb_mult=None,
               rsi_period=None, adx_period=None, width_window=None):
    """Всё, что нужно решению, посчитанное один раз на всю серию."""
    width_window = params.WIDTH_WINDOW if width_window is None else width_window
    mid, upper, lower = bollinger(close, bb_period, bb_mult)
    width = upper - lower
    ratio = np.full(len(width), np.nan)
    if width_window > 1:
        mean_width = pd.Series(width).rolling(width_window).mean().to_numpy()
        with np.errstate(divide='ignore', invalid='ignore'):
            ratio = np.where(mean_width > 0, width / mean_width, np.nan)
    return {
        'open': np.asarray(open_, dtype=float),
        'high': np.asarray(high, dtype=float),
        'low': np.asarray(low, dtype=float),
        'close': np.asarray(close, dtype=float),
        'mid': mid, 'upper': upper, 'lower': lower, 'width': width,
        'width_ratio': ratio,
        'rsi': rsi(close, rsi_period),
        'adx': adx(high, low, close, adx_period),
    }


def _ready(ind, at):
    for name in ('mid', 'upper', 'lower', 'rsi'):
        value = ind[name][at]
        if not np.isfinite(value):
            return False
    return ind['width'][at] > 0


def evaluate(ind, at, rsi_low=None, rsi_high=None, adx_max=None,
             max_width_ratio=None, entry_mode=None, reclaim_bars=None,
             rsi_mode=None):
    """
    Сетап на баре `at` либо (None, причина).

    Причина возвращается всегда — по ней видно, ЧТО именно отсеивает сигналы.
    Молчаливый None не отличает «условий не было» от «фильтр съел всё», и
    отладить такую стратегию можно только гаданием.
    """
    rsi_low = params.RSI_LOW if rsi_low is None else rsi_low
    rsi_high = params.RSI_HIGH if rsi_high is None else rsi_high
    adx_max = params.ADX_MAX if adx_max is None else adx_max
    max_width_ratio = (params.MAX_WIDTH_RATIO if max_width_ratio is None
                       else max_width_ratio)
    entry_mode = params.ENTRY_MODE if entry_mode is None else entry_mode
    reclaim_bars = params.RECLAIM_BARS if reclaim_bars is None else reclaim_bars
    rsi_mode = params.RSI_MODE if rsi_mode is None else rsi_mode

    if at < 2 or not _ready(ind, at):
        return None, 'данных не хватает'

    low, high, close = ind['low'], ind['high'], ind['close']
    upper, lower, mid = ind['upper'], ind['lower'], ind['mid']

    if entry_mode == 'reclaim':
        # Сначала выход за полосу, ЗАТЕМ закрытие обратно внутрь. Тень за
        # полосой ничего не сообщает: значение имеет то, где бар закрылся.
        start = max(0, at - reclaim_bars)
        pierced_low = any(low[k] < lower[k] for k in range(start, at + 1))
        pierced_high = any(high[k] > upper[k] for k in range(start, at + 1))
        inside = lower[at] < close[at] < upper[at]
        if not inside:
            return None, 'не закрылась внутрь полосы'
        if pierced_low and not pierced_high:
            side = LONG
        elif pierced_high and not pierced_low:
            side = SHORT
        else:
            return None, 'выхода за полосу не было'
    else:
        if low[at] <= lower[at]:
            side = LONG
        elif high[at] >= upper[at]:
            side = SHORT
        else:
            return None, 'полоса не задета'

    value = ind['rsi'][at]
    if rsi_mode == 'extreme':
        if side == LONG and value > rsi_low:
            return None, f'RSI {value:.0f} не подтвердил перепроданность'
        if side == SHORT and value < rsi_high:
            return None, f'RSI {value:.0f} не подтвердил перекупленность'
    elif rsi_mode == 'divergence':
        # Обратное прочтение: цена на нижней полосе, а импульс НЕ слаб.
        # Пробник на бычьем периоде даёт здесь резкую прибавку, но у неё есть
        # невинное объяснение: касание полосы — обычно ТЕНЬ, и высокий RSI при
        # ней означает, что закрытия растут, то есть мы в восходящем тренде.
        # Тогда «возврат к средней» — просто продолжение роста, и это уже не
        # возврат к среднему, а следование за трендом в другой одежде.
        # Проверяется медвежьим периодом, а не рассуждением.
        if side == LONG and value < rsi_low:
            return None, f'RSI {value:.0f} — импульс вниз, расхождения нет'
        if side == SHORT and value > rsi_high:
            return None, f'RSI {value:.0f} — импульс вверх, расхождения нет'
    elif rsi_mode == 'neutral':
        if not (rsi_low <= value <= rsi_high):
            return None, f'RSI {value:.0f} вне середины'
    elif rsi_mode != 'off':
        return None, f'неизвестный режим RSI: {rsi_mode}'

    if adx_max > 0:
        strength = ind['adx'][at]
        if not np.isfinite(strength):
            return None, 'ADX ещё не готов'
        if strength > adx_max:
            return None, f'ADX {strength:.0f} — тренд, возврата не ждём'

    if max_width_ratio > 0:
        ratio = ind['width_ratio'][at]
        if not np.isfinite(ratio):
            return None, 'ширина полос ещё не готова'
        if ratio > max_width_ratio:
            return None, f'полосы расширяются ({ratio:.2f})'

    return {
        'direction': side,
        'band': float(lower[at] if side == LONG else upper[at]),
        'mid': float(mid[at]),
        'half_width': float(ind['width'][at] / 2),
        'rsi': float(value),
        'adx': float(ind['adx'][at]) if np.isfinite(ind['adx'][at]) else None,
        'width_ratio': (float(ind['width_ratio'][at])
                        if np.isfinite(ind['width_ratio'][at]) else None),
        'close': float(close[at]),
        'at': int(at),
        'entry_mode': entry_mode,
    }, None


def build_trade(setup, target_frac=None, stop_frac=None, min_rr=None,
                min_stop_pct=None, thin_stop=None):
    """
    Вход, стоп, цель. None — геометрия не годится.

    Вход в режиме 'touch' стоит НА полосе: заявка лимитная, цена приходит к ней
    сама. В режиме 'reclaim' входим по цене закрытия бара подтверждения — она
    уже хуже, и это честная плата за подтверждённый отбой.

    Цель на средней линии, а не на ближайшем уровне: короткая цель убила сетку
    в коридоре, где при отношении риска к прибыли 0.38 безубыток требовал 72%
    попаданий.
    """
    target_frac = params.TARGET_FRAC if target_frac is None else target_frac
    stop_frac = params.STOP_FRAC if stop_frac is None else stop_frac
    min_rr = params.MIN_RR if min_rr is None else min_rr
    min_stop_pct = params.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct
    thin_stop = params.THIN_STOP if thin_stop is None else thin_stop

    long_side = setup['direction'] == LONG
    half = setup['half_width']
    if half <= 0:
        return None

    entry = setup['band'] if setup['entry_mode'] == 'touch' else setup['close']
    pad = stop_frac * half
    stop = entry - pad if long_side else entry + pad
    reach = half * target_frac
    target = entry + reach if long_side else entry - reach

    distance = abs(entry - stop)
    floor = entry * min_stop_pct / 100
    if distance < floor:
        # РАЗВИЛКА, КОТОРУЮ НЕЛЬЗЯ ПОДРАЗУМЕВАТЬ. Пол по стопу пришёл из
        # стратегий с широкой геометрией, где он срабатывал изредка. Здесь
        # канал узкий, и пол упирается почти всегда: естественный стоп 0.23%
        # цены расширяется до 0.4%, а цель остаётся на 0.46% — отношение риска
        # к прибыли падает с 2.0 до 1.15, и стратегия меряется не та, что
        # задумана.
        #
        # 'widen' — расширить стоп, приняв худшее отношение;
        # 'skip'  — не брать сетап вовсе: слишком узкий канал не окупает круг
        #           комиссий, и это тот же довод, что и у самого пола.
        # Что верно — решает замер, поэтому выбор вынесен в параметр.
        if thin_stop == 'skip':
            return None
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
