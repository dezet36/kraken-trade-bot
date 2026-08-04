"""
Режим рынка и размер позиции.

Замер на двух независимых периодах дал разницу, которая не растворилась ни
при разбивке по направлению, ни при переходе на причинную разметку:

    режим            сделок   R/сделку   интервал среднего
    рост                 72     -0.090   [-0.566; +0.470]
    падение              77     +0.115   [-0.386; +0.664]
    боковик             349     +0.349   [+0.045; +0.672]   <- только здесь
    режим неизвестен    334     +0.319   [+0.040; +0.619]      не через ноль

В выраженном тренде цена не возвращается в зону интереса, и вся конструкция
работает вхолостую — но несёт при этом полный риск. Направление при этом
определяется верно: в падении 69-81% сделок шорты, в росте 81-82% лонги.
Дело не в стороне сделки, а в том, что возвратной торговле в тренде нечего
ловить.

Отсюда правило: в трендовом режиме риск на сделку умножается на
REGIME_RISK_SCALE. Не запрет — именно уменьшение: полный запрет проверялся и
оказался хуже (освободившиеся слоты портфеля достаются худшим сетапам).

Что дал контроль. Любое сокращение риска механически уменьшает просадку,
поэтому мерилось против равномерного сокращения на всех сделках. При
совпавшей просадке адресный вариант даёт заметно больше:

    бык:      в тренде 0.50 -> DD 18.0%, доход +408.9%
              равномерно 0.90 -> DD 18.1%, доход +362.8%
    медведь:  в тренде 0.25 -> DD 28.3%, доход +38.9%
              равномерно 0.75 -> DD 28.3%, доход +29.7%

Одинаковая просадка, разный доход. Работает адресность, а не сам факт
уменьшения размера.

ПРИЧИННОСТЬ. Порог направленности считается по значениям, известным на этот
день, а не по всему периоду. Это не педантизм: разбор изначально размечался
порогом-квантилем по всей истории, и такого порога живой бот не знает.
Правило, настроенное на подсмотренный порог, вживую не воспроизводится.
Пока накоплено меньше REGIME_MIN_HISTORY значений, режим не определён и
размер остаётся полным — молчать там, где статистики ещё нет, обязательно.
"""

import numpy as np

from . import params

TREND_UP = 'рост'
TREND_DOWN = 'падение'
RANGE = 'боковик'
UNKNOWN = None


def efficiency_ratio(closes, window):
    """
    Коэффициент эффективности Кауфмана на последнем баре.

    Отношение пройденного расстояния к длине пути. Единица — движение по
    прямой, ноль — топтание на месте. Именно это отличает тренд от боковика
    лучше, чем наклон скользящей: наклон одинаков и у ровного хода, и у пилы
    с тем же итогом.
    """
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

    Порог берётся по прошлым значениям ER, текущее в его расчёт не входит —
    иначе сегодняшний день влиял бы на собственную разметку.
    """
    window = params.REGIME_ER_WINDOW if window is None else window
    quantile = params.REGIME_ER_QUANTILE if quantile is None else quantile
    min_history = params.REGIME_MIN_HISTORY if min_history is None else min_history

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


def risk_multiplier(regime):
    """
    Множитель риска для режима. Единица — размер не меняется.

    Множитель НИКОГДА не превышает единицы: правило умеет только уменьшать
    ставку. Увеличение размера по сигналу режима не проверялось, а
    непроверенное увеличение риска — это не улучшение, а новый риск.
    """
    if regime in (TREND_UP, TREND_DOWN):
        return min(1.0, max(0.0, float(params.REGIME_RISK_SCALE)))
    return 1.0


def describe(regime, er, threshold, multiplier):
    """Строка для журнала и дашборда."""
    if regime is UNKNOWN:
        return (f'режим неизвестен (мало истории), риск полный'
                if er is None else
                f'режим неизвестен (ER {er:.3f}, мало истории), риск полный')
    tail = '' if multiplier >= 1.0 else f', риск ×{multiplier:.2f}'
    return f'{regime} (ER {er:.3f} при пороге {threshold:.3f}){tail}'
