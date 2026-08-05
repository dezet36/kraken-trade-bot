"""
Режим рынка: состояние, общее для всех стратегий.

ПОЧЕМУ ОТДЕЛЬНЫМ МОДУЛЕМ, А НЕ ВНУТРИ SMC. Раньше это лежало в smc/regime.py.
Пока стратегия была одна, разницы не было; с появлением второй пришлось бы
импортировать чужой пакет ради общей вещи — а по принятому в проекте правилу
стратегии не опираются друг на друга. Направленность рынка не принадлежит ни
одной из них: это свойство рынка.

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО НЕТ. Здесь только ОПРЕДЕЛЕНИЕ режима. Реакция на него —
насколько урезать размер позиции — остаётся за стратегией: у SMC преимущество
в боковике, у уровней в падении, и общий множитель обслужил бы обе плохо.

ЧИСЛА И ЗАМЕР (по SMC, два независимых периода):

    режим            сделок   R/сделку   интервал среднего
    рост                 72     -0.090   [-0.566; +0.470]
    падение              77     +0.115   [-0.386; +0.664]
    боковик             349     +0.349   [+0.045; +0.672]   <- только здесь
    режим неизвестен    334     +0.319   [+0.040; +0.619]      не через ноль

ПРИЧИННОСТЬ. Порог направленности считается по значениям, известным на этот
день, а не квантилем по всей истории. Это не педантизм: разбор изначально
размечался порогом по всему периоду, и такого порога живой бот не знает.
Правило, настроенное на подсмотренный порог, вживую не воспроизводится.
Пока накоплено меньше MIN_HISTORY значений, режим не определён.
"""

import os

import numpy as np

TREND_UP = 'рост'
TREND_DOWN = 'падение'
RANGE = 'боковик'
UNKNOWN = None


def _f(name, default):
    try:
        return float(os.getenv(f'REGIME_{name}', default))
    except (TypeError, ValueError):
        return default


def _i(name, default):
    try:
        return int(os.getenv(f'REGIME_{name}', default))
    except (TypeError, ValueError):
        return default


# Окно коэффициента эффективности Кауфмана, дней.
ER_WINDOW = _i('ER_WINDOW', 30)

# Верхняя треть значений считается направленным рынком. Фиксированный порог
# вроде 0.35 загонял 83% дней в одну корзину: сравнивать при таком перекосе
# нечего.
ER_QUANTILE = _f('ER_QUANTILE', 0.667)

# Дней истории до первой разметки. Раньше этого режим не определён.
MIN_HISTORY = _i('MIN_HISTORY', 180)

# По какому инструменту меряется направленность рынка.
SYMBOL = os.getenv('REGIME_SYMBOL', 'BTCUSDT')


def efficiency_ratio(closes, window=None):
    """
    Коэффициент эффективности Кауфмана на последнем баре.

    Отношение пройденного расстояния к длине пути. Единица — движение по
    прямой, ноль — топтание на месте. Отличает тренд от боковика лучше, чем
    наклон скользящей: наклон одинаков и у ровного хода, и у пилы с тем же
    итогом.
    """
    window = ER_WINDOW if window is None else window
    closes = np.asarray(closes, dtype=float)
    if len(closes) <= window:
        return None, None
    moved = closes[-1] - closes[-1 - window]
    path = np.abs(np.diff(closes[-1 - window:])).sum()
    if path <= 0:
        return 0.0, moved
    return abs(moved) / path, moved


def _history(closes, window):
    """Все значения ER по закрытым барам, кроме последнего."""
    closes = np.asarray(closes, dtype=float)
    steps = np.abs(np.diff(closes, prepend=closes[0]))
    values = []
    for i in range(window, len(closes) - 1):
        moved = closes[i] - closes[i - window]
        path = steps[i - window + 1:i + 1].sum()
        values.append(abs(moved) / path if path > 0 else 0.0)
    return values


def classify(closes, window=None, quantile=None, min_history=None):
    """
    Режим на последнем ЗАКРЫТОМ баре. Возвращает (режим, er, порог).

    Порог берётся по прошлым значениям ER; текущее в его расчёт не входит —
    иначе сегодняшний день влиял бы на собственную разметку.
    """
    window = ER_WINDOW if window is None else window
    quantile = ER_QUANTILE if quantile is None else quantile
    min_history = MIN_HISTORY if min_history is None else min_history

    er, moved = efficiency_ratio(closes, window)
    if er is None:
        return UNKNOWN, None, None

    past = _history(closes, window)
    if len(past) < min_history:
        return UNKNOWN, er, None

    threshold = float(np.quantile(past, quantile))
    if er < threshold:
        return RANGE, er, threshold
    return (TREND_UP if moved > 0 else TREND_DOWN), er, threshold


def risk_multiplier(regime, trend_scale, range_scale=1.0):
    """
    Множитель риска для режима. Реакция на режим — дело СТРАТЕГИИ, поэтому
    коэффициенты передаются, а не берутся отсюда.

    Множитель никогда не превышает единицы: правило умеет только уменьшать
    ставку. Увеличение размера по сигналу режима не проверялось, а
    непроверенное увеличение риска — это не улучшение, а новый риск.
    """
    if regime in (TREND_UP, TREND_DOWN):
        value = trend_scale
    elif regime == RANGE:
        value = range_scale
    else:
        return 1.0
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def describe(regime, er, threshold, multiplier=1.0):
    """Строка для журнала и дашборда."""
    if regime is UNKNOWN:
        return ('режим неизвестен (мало истории), риск полный' if er is None
                else f'режим неизвестен (ER {er:.3f}, мало истории), риск полный')
    tail = '' if multiplier >= 1.0 else f', риск ×{multiplier:.2f}'
    return f'{regime} (ER {er:.3f} при пороге {threshold:.3f}){tail}'
