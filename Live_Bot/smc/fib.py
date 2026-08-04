"""
Фибоначчи — вспомогательный слой SMC (§10 и §22 методички).

В этой стратегии Фибоначчи НЕ самостоятельный сигнал. Методичка §10.1 прямо
предупреждает: «сами по себе линии Фибоначчи не являются уровнями
поддержки/сопротивления — торговать только на их основе, без привязки к
конкретным зонам на графике (OB, FVG, BB, MB), не стоит».

Слой отвечает за три вещи:
    1. premium / discount / equilibrium — умные деньги покупают со скидкой и
       продают с наценкой (§10.1). Это фильтр, где допустим POI.
    2. OTE (Optimal Trade Entry) — зона 0.62/0.705/0.79 с наибольшим
       математическим ожиданием разворота.
    3. Цели по отрицательным уровням сетки (§10.2) и классическая сетка §22.

Сетка тянется строго от начала до конца импульса (§22.3, правило 1).
"""

from . import params

BULLISH = 'BULLISH'
BEARISH = 'BEARISH'

PREMIUM = 'PREMIUM'
DISCOUNT = 'DISCOUNT'
EQUILIBRIUM = 'EQUILIBRIUM'


def retracement(leg, ratio):
    """
    Цена уровня коррекции для доли `ratio` (0 = конец импульса, 1 = начало).

    Для бычьей ноги (low -> high) коррекция отсчитывается вниз от максимума,
    для медвежьей — вверх от минимума.
    """
    start = leg['start']['price']
    end = leg['end']['price']
    size = abs(end - start)

    if leg['direction'] == BULLISH:
        return end - size * ratio
    return end + size * ratio


def extension(leg, ratio):
    """
    Цель по отрицательному уровню сетки (§10.2): −0.27 / −0.62 / −1.0
    отсчитываются ЗА конец импульса в направлении движения.
    """
    start = leg['start']['price']
    end = leg['end']['price']
    size = abs(end - start)

    if leg['direction'] == BULLISH:
        return end + size * ratio
    return end - size * ratio


def levels(leg):
    """Полная сетка коррекции по ноге — удобно для логов и графиков."""
    grid = {}
    for ratio in (0.0, 0.236, params.FIB_ZONE_SHALLOW, params.FIB_EQUILIBRIUM,
                  params.FIB_ZONE_DEEP, params.FIB_DEEP_RETRACE_LO,
                  params.FIB_DEEP_RETRACE_HI, 1.0):
        grid[round(ratio, 3)] = retracement(leg, ratio)
    return grid


def equilibrium(leg):
    """Справедливая цена — 50% ноги (§10.1)."""
    return retracement(leg, params.FIB_EQUILIBRIUM)


def market_side(price, leg):
    """
    Где находится цена относительно equilibrium: premium, discount или на нём.

    §10.1: для лонга рассматриваем поведение цены НИЖЕ 0.5 (дисконт),
    для шорта — ВЫШЕ 0.5 (премиум).
    """
    eq = equilibrium(leg)
    if abs(price - eq) / eq < 1e-9:
        return EQUILIBRIUM

    if leg['direction'] == BULLISH:
        # Нога вверх: ниже equilibrium — дисконт (зона покупок)
        return DISCOUNT if price < eq else PREMIUM
    # Нога вниз: выше equilibrium — премиум (зона продаж)
    return PREMIUM if price > eq else DISCOUNT


def is_valid_side(price, leg, direction):
    """
    Проверка правила «покупать со скидкой, продавать с наценкой» (§10.1).

    Для лонга цена должна быть в дисконте, для шорта — в премиуме.
    """
    side = market_side(price, leg)
    if side == EQUILIBRIUM:
        return True
    return side == (DISCOUNT if direction == BULLISH else PREMIUM)


def zone_of_interest(leg):
    """
    Зона интереса по сетке — 38.2%-61.8% коррекции (§22.1).

    Возвращает (bottom, top) в ценах, независимо от направления ноги.
    """
    a = retracement(leg, params.FIB_ZONE_SHALLOW)
    b = retracement(leg, params.FIB_ZONE_DEEP)
    return (min(a, b), max(a, b))


def deep_zone(leg):
    """Зона глубокой коррекции 78.6%-88.6% (§22.1)."""
    a = retracement(leg, params.FIB_DEEP_RETRACE_LO)
    b = retracement(leg, params.FIB_DEEP_RETRACE_HI)
    return (min(a, b), max(a, b))


def ote_zone(leg):
    """
    Optimal Trade Entry — 0.62-0.79 коррекции (§10.1).

    Возвращает (bottom, top): зона с наибольшим матожиданием разворота.
    """
    lo = retracement(leg, params.FIB_OTE[0])
    hi = retracement(leg, params.FIB_OTE[-1])
    return (min(lo, hi), max(lo, hi))


def in_ote(price, leg):
    """Попадает ли цена в зону OTE."""
    bottom, top = ote_zone(leg)
    return bottom <= price <= top


def invalidation_level(leg):
    """
    Уровень инвалидации сетапа — 88.6% коррекции (§22.2, п.5).

    Закрепление цены телом свечи за этим уровнем отменяет оба сценария сетки.
    """
    return retracement(leg, params.FIB_DEEP_RETRACE_HI)


def is_invalidated(price, leg):
    """Сетка отменена: цена ушла за 88.6%."""
    level = invalidation_level(leg)
    if leg['direction'] == BULLISH:
        return price < level
    return price > level


def targets(leg, entry=None):
    """
    Цели по расширениям (§10.2): −0.27 = тейк 1, −0.62 = тейк 2, −1.0 = тейк 3.

    Возвращает список цен в порядке достижения. Если передан entry, цели,
    оказавшиеся позади входа, отбрасываются — иначе тейк сработал бы мгновенно.
    """
    out = []
    for ratio in params.FIB_TARGETS:
        price = extension(leg, ratio)
        if entry is not None:
            if leg['direction'] == BULLISH and price <= entry:
                continue
            if leg['direction'] == BEARISH and price >= entry:
                continue
        out.append(price)
    return out


def law_of_effort(leg, correction_bars):
    """
    Закон силы (§10.3): время формирования импульса должно быть МЕНЬШЕ
    времени формирования коррекции.

    Если цена подходит к зоне интереса медленнее, чем шёл импульс, моментум на
    стороне трейдера — это дополнительный фактор для входа.

    correction_bars — сколько свечей длится текущая коррекция.
    """
    impulse_bars = max(1, leg['end']['index'] - leg['start']['index'])
    return correction_bars > impulse_bars
