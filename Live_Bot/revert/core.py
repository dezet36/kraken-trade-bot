"""
Возврат после резкого движения: поиск сетапа.

ОДНА реализация на замер и на бой — правило проекта, нарушенное однажды у
стратегии уровней и стоившее месяца недостоверных наблюдений.

ЧТО ИЩЕТСЯ
    1. Резкое НАПРАВЛЕННОЕ движение за последние N баров.
    2. Лимит против него, ЗА экстремумом: импульс должен продлиться ещё
       немного, чтобы нас набрать.
    3. Цель — доля импульса обратно, стоп — за продолжение.

ПОЧЕМУ ЛИМИТ СТАВИТСЯ ЗА ЭКСТРЕМУМ, А НЕ НА НЁМ. Две причины, и вторая
важнее. Первая: заявка, стоящая по ходу движения, исполняется мейкером —
цена приходит к ней сама. Вторая: если движение выдохлось, не дойдя до нашей
цены, сделки не будет вовсе — а это ровно те случаи, где возврат уже начался
без нас и ловить нечего.

ЧЕГО ЗДЕСЬ НЕТ. Заглядывания вперёд. Всё считается по барам строго до
сигнального включительно.
"""

import numpy as np

from . import params

LONG, SHORT = 'LONG', 'SHORT'


def atr(high, low, close, at, period=14):
    """Средний истинный диапазон по барам до `at` включительно."""
    start = max(1, at - period + 1)
    if at < 1:
        return 0.0
    highs, lows = high[start:at + 1], low[start:at + 1]
    prev = close[start - 1:at]
    n = min(len(highs), len(lows), len(prev))
    if n == 0:
        return 0.0
    highs, lows, prev = highs[-n:], lows[-n:], prev[-n:]
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - prev), np.abs(lows - prev)))
    return float(np.mean(tr)) if len(tr) else 0.0


def find_setup(open_, high, low, close, at):
    """Сетап на баре `at` либо None."""
    window = params.IMPULSE_BARS
    if at < window + 20:
        return None
    atr_now = atr(high, low, close, at)
    if atr_now <= 0:
        return None

    start = close[at - window]
    end = close[at]
    move = end - start
    size = abs(move)
    if size < params.IMPULSE_ATR * atr_now:
        return None

    # Направленность: движение, а не размах. Без этого сетапом считался бы
    # любой всплеск волатильности, вернувшийся туда же, откуда вышел.
    seg = slice(at - window + 1, at + 1)
    if move > 0:
        directed = int(np.sum(close[seg] > open_[seg]))
    else:
        directed = int(np.sum(close[seg] < open_[seg]))
    if directed / window < params.DIRECTIONAL:
        return None

    # Встаём ПРОТИВ движения.
    side = SHORT if move > 0 else LONG
    extreme = float(np.max(high[seg])) if move > 0 else float(np.min(low[seg]))
    offset = params.ENTRY_OFFSET_ATR * atr_now
    entry = extreme + offset if side == SHORT else extreme - offset

    return {
        'direction': side,
        'entry': entry,
        'extreme': extreme,
        'impulse': size,
        'atr': atr_now,
        'close': float(end),
        'directed': directed / window,
    }


def build_trade(setup, stop_atr=None, target_frac=None, min_rr=None):
    """
    Стоп, цель и отношение к риску. Возвращает None, если геометрия не годится.

    Стоп считается от ВХОДА, а не от экстремума: вход стоит за экстремумом, и
    отсчёт от экстремума дал бы стоп тем ближе, чем дальше мы зашли, — то есть
    самые агрессивные входы получали бы самые тесные стопы.
    """
    stop_atr = params.STOP_ATR if stop_atr is None else stop_atr
    target_frac = params.TARGET_FRAC if target_frac is None else target_frac
    min_rr = params.MIN_RR if min_rr is None else min_rr

    entry = setup['entry']
    distance = max(stop_atr * setup['atr'],
                   entry * params.MIN_STOP_PCT / 100)
    if setup['direction'] == LONG:
        stop = entry - distance
        target = entry + setup['impulse'] * target_frac
    else:
        stop = entry + distance
        target = entry - setup['impulse'] * target_frac

    rr = abs(target - entry) / distance
    if rr < min_rr:
        return None
    return {'entry': entry, 'stop': stop, 'target': target, 'rr': rr,
            'stop_pct': distance / entry * 100}
