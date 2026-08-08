"""
Полная разметка импульса 1-2-3-4-5 по всем трём строгим правилам.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ core.find_wave. Там размечались только волна 1 и волна 2
(два-три пивота) и проверялось единственное правило — первое. Здесь берётся
окно из ШЕСТИ пивотов, то есть весь импульс целиком, и применяются все три
правила, включая те два, что раньше были отрезаны сознательно:

    правило 2 — волна 3 не самая короткая из 1, 3 и 5;
    правило 3 — волна 4 не заходит в ценовую область волны 1.

ЗАЧЕМ ЭТО ВООБЩЕ ДЕЛАТЬ, ЕСЛИ ТЕМА ЗАКРЫТА. Прошлый замер отверг вход в начале
волны 3 — сетап, где правила 2 и 3 неприменимы в принципе: волн 3, 4 и 5 ещё
нет. То есть отвергнута была ОДНА конструкция, а не теория. Полная разметка
даёт другой сетап (вход в конце волны 4 с расчётом на волну 5) и другой отбор,
и это честно проверяемое, ещё не проверенное утверждение.

ЦЕНА ПОЛНОТЫ ИЗВЕСТНА ЗАРАНЕЕ. Шесть подтверждённых пивотов вместо двух — это
и много меньше сетапов, и куда большее запаздывание: к концу волны 4 движение,
ради которого всё затевалось, состоялось на три четверти. Прошлый пробник
измерил, что подтверждение ОДНОГО пивота стоит около половины длины колена;
здесь таких ожиданий пять подряд.

ЧЕГО ЗДЕСЬ НЕТ. Диагональных треугольников, усечений, комбинаций W-X-Y. Первые
дают официальное исключение из правила 3, остальные, по признанию самого
источника, «практически не поддаются надёжной алгоритмической разметке в
реальном времени». Разметка без них строже, чем у человека, — и это осознанный
выбор: лучше отвергнуть годный сетап, чем принять любой.
"""

import numpy as np

from . import params

LONG, SHORT = 'LONG', 'SHORT'
HIGH, LOW = 'H', 'L'

# Типичные соотношения из таблицы источника. Используются как ШКАЛА БЛИЗОСТИ, а
# не как фильтр: «если не 61.8% — не считается» отвергло бы почти всё, потому
# что это статистические тенденции, а не физический закон.
TYPICAL = {
    'w2_of_w1': (0.500, 0.618, 0.786),
    'w3_of_w1': (1.618, 2.618),
    'w4_of_w3': (0.236, 0.382, 0.500),
    'w5_of_w1': (0.618, 1.000, 1.618),
}


def _closeness(value, targets, tolerance=0.15):
    """
    Насколько отношение близко к ближайшему типичному, от 0 до 1.

    Допуск задан ОТНОСИТЕЛЬНЫЙ: промах на 0.05 при цели 0.5 и при цели 2.618 —
    это разная точность, и мерить их одной линейкой значило бы требовать от
    расширений недостижимого совпадения.
    """
    if value is None or not np.isfinite(value) or value <= 0:
        return 0.0
    best = 0.0
    for target in targets:
        miss = abs(value - target) / target
        best = max(best, max(0.0, 1.0 - miss / tolerance))
    return best


def fib_score(ratios):
    """
    Уверенность разметки: среднее по близости отношений к типичным.

    Возвращает 0..1. Это единственная количественно проверяемая часть теории,
    и потому единственная, которую вообще имеет смысл превращать в число.
    """
    parts = [_closeness(ratios.get(name), targets)
             for name, targets in TYPICAL.items()]
    return float(np.mean(parts)) if parts else 0.0


def _alternation(w2_depth, w4_depth):
    """
    Чередование: если волна 2 глубокая, волна 4 обычно мелкая, и наоборот.

    Рекомендация, а не правило, поэтому идёт бонусом к уверенности, а не
    отказом. Возвращает 0..1 по тому, насколько глубины РАЗЛИЧАЮТСЯ.
    """
    if not w2_depth or not w4_depth:
        return 0.0
    lo, hi = sorted((w2_depth, w4_depth))
    if hi <= 0:
        return 0.0
    return float(min(1.0, (hi - lo) / hi))


def find_impulse(pivots, k, atr, min_wave_atr=None, tolerance=None,
                 allow_truncation=None):
    """
    Импульс 1-2-3-4-5, заканчивающийся пивотом номер `k`. None — не складывается.

    Окно: pivots[k-5 .. k] — шесть точек, то есть начало волны 1 и концы волн
    1..5. Решение принимается на баре confirmed_at последнего пивота: ни одно
    поле результата не читает данные после него.
    """
    min_wave_atr = params.MIN_WAVE_ATR if min_wave_atr is None else min_wave_atr
    tolerance = params.FIB_TOLERANCE if tolerance is None else tolerance
    allow_truncation = (params.ALLOW_TRUNCATION if allow_truncation is None
                        else allow_truncation)
    if k < 5:
        return None

    p = pivots[k - 5:k + 1]
    # Зигзаг чередует стороны, но полагаться на это молча нельзя.
    kinds = [point['kind'] for point in p]
    if any(a == b for a, b in zip(kinds, kinds[1:])):
        return None

    up = p[0]['kind'] == LOW
    direction = LONG if up else SHORT
    price = [point['price'] for point in p]

    # Длины волн БЕЗ знака: правила говорят о протяжённости, а не о стороне.
    w1 = abs(price[1] - price[0])
    w2 = abs(price[2] - price[1])
    w3 = abs(price[3] - price[2])
    w4 = abs(price[4] - price[3])
    w5 = abs(price[5] - price[4])
    if min(w1, w2, w3, w4, w5) <= 0:
        return None

    at = p[5]['confirmed_at']
    atr_now = float(atr[at]) if at < len(atr) and np.isfinite(atr[at]) else 0.0
    if atr_now <= 0 or w1 < min_wave_atr * atr_now:
        return None

    # ── Правило 1: волна 2 не откатывает больше 100% волны 1 ────────────────
    if up and price[2] <= price[0]:
        return None
    if not up and price[2] >= price[0]:
        return None

    # ── Правило 2: волна 3 не самая короткая из 1, 3, 5 ─────────────────────
    if w3 < w1 and w3 < w5:
        return None

    # ── Правило 3: волна 4 не заходит в ценовую область волны 1 ─────────────
    # Область волны 1 — отрезок [price[0], price[1]]. Для восходящего импульса
    # нарушение — это заход волны 4 НИЖЕ вершины волны 1.
    if up and price[4] <= price[1]:
        return None
    if not up and price[4] >= price[1]:
        return None

    # ── Усечение волны 5 ────────────────────────────────────────────────────
    # Пятая волна не превысила вершину третьей. Признанный, но редкий случай и
    # признак слабости тренда. Отдельным флагом, потому что для сетапа «вход в
    # конце волны 4» усечение — это провал цели, а не деталь разметки.
    truncated = (price[5] <= price[3]) if up else (price[5] >= price[3])
    if truncated and not allow_truncation:
        return None

    ratios = {
        'w2_of_w1': w2 / w1,
        'w3_of_w1': w3 / w1,
        'w4_of_w3': w4 / w3,
        'w5_of_w1': w5 / w1,
    }
    score = fib_score(ratios)
    extended = max(w1, w3, w5)
    return {
        'direction': direction,
        'points': p,
        'at': int(at),
        'atr': atr_now,
        'waves': {'w1': w1, 'w2': w2, 'w3': w3, 'w4': w4, 'w5': w5},
        'ratios': ratios,
        'score': score,
        'alternation': _alternation(w2 / w1, w4 / w3),
        'truncated': bool(truncated),
        # Какая из мотивных волн растянута. Источник утверждает, что на крипте
        # чаще всего третья; это утверждение здесь и записывается в поле, чтобы
        # потом можно было проверить его на наших данных, а не поверить.
        'extended': ('w1' if extended == w1 else
                     'w3' if extended == w3 else 'w5'),
        'lag': int(at - p[5]['index']),
    }


def wave_four_entry(impulse, stop_pad_atr=None, target_mode=None, min_rr=None,
                    min_stop_pct=None, price_now=None):
    """
    Вход в конце волны 4 с расчётом на волну 5.

    ПОЧЕМУ ИМЕННО ЭТОТ СЕТАП, А НЕ ВХОД В ВОЛНУ 3. Волна 3 размечается только
    предположительно: волн 4 и 5 ещё нет, и правила 2 и 3 применить не к чему —
    именно этот сетап прошлый замер и отверг. К концу волны 4 обе проверки уже
    выполнимы, и разметка перестаёт быть догадкой.

    ЦЕНА ТА ЖЕ, ЧТО И ВСЕГДА: движение состоялось на три четверти, и остаётся
    последняя волна — по признанию самой теории, самая слабая из трёх мотивных.

    `price_now` обязателен по той же причине, что и в core.build_trade: конец
    волны 4 известен только ПОСЛЕ разворота от него, и войти по его цене нельзя.
    """
    stop_pad_atr = (params.STOP_PAD_ATR if stop_pad_atr is None
                    else stop_pad_atr)
    target_mode = params.TARGET_MODE if target_mode is None else target_mode
    min_rr = params.MIN_RR if min_rr is None else min_rr
    min_stop_pct = params.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct

    p = impulse['points']
    up = impulse['direction'] == LONG
    price = [point['price'] for point in p]
    waves = impulse['waves']
    pad = stop_pad_atr * impulse['atr']

    entry = float(price_now)
    # Стоп за концом волны 4: уход туда означает, что волна 4 не закончилась,
    # а при заходе за вершину волны 1 разметка нарушает правило 3 и умирает.
    stop = price[4] - pad if up else price[4] + pad

    if target_mode == 'equality':
        # Правило равенства: волна 5 повторяет волну 1.
        reach = waves['w1']
    elif target_mode == 'w1_618':
        reach = waves['w1'] * 0.618
    else:
        # Доля чистого хода волн 1-3 — вариант из таблицы для случая, когда
        # волна 3 растянута.
        reach = abs(price[3] - price[0]) * 0.382
    target = price[4] + reach if up else price[4] - reach

    if up and not (stop < entry):
        return None
    if not up and not (entry < stop):
        return None

    distance = abs(entry - stop)
    floor = entry * min_stop_pct / 100
    if distance < floor:
        stop = entry - floor if up else entry + floor
        distance = floor
    if distance <= 0:
        return None
    if (target - entry > 0) != up:
        return None

    rr = abs(target - entry) / distance
    if rr < min_rr:
        return None
    return {'entry': entry, 'stop': stop, 'target': target, 'rr': rr,
            'stop_pct': distance / entry * 100}
