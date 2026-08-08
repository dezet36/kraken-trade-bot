"""
Человеческие названия для внутренних обозначений стратегий.

Внутри кода факторы, зоны и причины выхода живут техническими именами:
`htf_bias_aligned`, `ORDER_BLOCK`, `SL_after_TP2`. Это правильно — по ним
считается статистика и пишется анализ. Но в журнале сделок и на дашборде
такие имена заставляют держать словарь в голове, а сделку надо понимать с
одного взгляда: почему вошли и чего не хватило.

Перевод живёт здесь, а не в интерфейсе, по двум причинам: он попадает в
выгрузку CSV (её открывают в Excel, а не в браузере), и он один на все
способы показать сделку — дашборд, Telegram, журнал.

Технические имена при этом НЕ заменяются: они остаются в отдельных колонках,
иначе разбор вкладов (research/smc_attribution.py) перестал бы работать.
"""

# Факторы подтверждения SMC (§23 методички)
FACTORS = {
    'htf_bias_aligned': 'по тренду старшего таймфрейма',
    'premium_discount': 'вход со скидкой к справедливой цене',
    'poi_fresh': 'зона ещё не тронута',
    'ote_zone': 'вход в зоне OTE (0.62–0.79)',
    'structure_break': 'слом структуры в нашу сторону',
    'fvg_present': 'рядом незакрытый имбаланс',
    'liquidity_swept': 'ликвидность снята перед входом',
    'law_of_effort': 'коррекция медленнее импульса',
    'killzone': 'вход в торговую сессию',
}

# Типы зон интереса (§5)
POI_TYPES = {
    'ORDER_BLOCK': 'ордер-блок',
    'BREAKER': 'брейкер',
    'MITIGATION': 'митигация',
    'WICK': 'зона тени',
    'FVG': 'имбаланс',
}

# События структуры (§2.2-2.5). Подпись на графике должна читаться без
# знания жаргона: аббревиатура остаётся в скобках для тех, кто его знает.
STRUCTURE_EVENTS = {
    'BOS': 'слом структуры (BOS)',
    'CHOCH': 'смена характера (CHoCH)',
    'MBOS': 'слом внутренней структуры (mBOS)',
    'MCHOCH': 'смена характера внутри (mCHoCH)',
}

# Стороны снятой ликвидности. BSL и SSL — то, ЧТО снимали: скопление стопов
# над максимумами либо под минимумами.
LIQUIDITY_SIDES = {
    'BSL': 'ликвидность над максимумами',
    'SSL': 'ликвидность под минимумами',
}

# Зоны фибо-стратегии
ZONES = {
    'Zone_A': 'зона A (коррекция 38.2–61.8%)',
    'Zone_B': 'зона B (глубокая коррекция 78.6–88.6%)',
}

TRENDS = {
    'BULLISH': 'восходящий',
    'BEARISH': 'нисходящий',
    'NEUTRAL': 'нейтральный',
}

DIRECTIONS = {'LONG': 'лонг', 'SHORT': 'шорт',
              'BULLISH': 'лонг', 'BEARISH': 'шорт'}

# Причины выхода из позиции
EXITS = {
    'SL': 'стоп-лосс',
    'BE': 'безубыток',
    'TIME': 'тайм-стоп',
    'TIME_STOP': 'тайм-стоп',
    'MANUAL': 'закрыто вручную',
    'EXT': 'закрыто вне бота',
    'TP1': 'первая цель',
    'TP2': 'вторая цель',
    'TP3': 'третья цель',
}


def factor(name):
    return FACTORS.get(name, name)


def poi_type(name):
    return POI_TYPES.get(name, name or '—')


def structure_event(name):
    return STRUCTURE_EVENTS.get(name, name or '—')


def liquidity_side(name):
    return LIQUIDITY_SIDES.get(name, name or '—')


def zone(name):
    return ZONES.get(name, name or '—')


def trend(name):
    return TRENDS.get(name, name or '—')


def direction(name):
    return DIRECTIONS.get(name, name or '—')


def exit_reason(name):
    """
    Причина выхода. Составные вида «SL_after_TP2» разбираются: стоп после
    двух взятых целей — это не убыток, а частично зафиксированная прибыль,
    и путать их в статистике нельзя.
    """
    if not name:
        return '—'
    text = str(name)
    if text in EXITS:
        return EXITS[text]
    if '_after_' in text:
        base, after = text.split('_after_', 1)
        taken = EXITS.get(after, after)
        return f'{EXITS.get(base, base)} после того, как взята {taken.lower()}'
    return text


def confirmations(factors):
    """
    Делит факторы на сработавшие и нет.

    Возвращает (сработало, не хватило) — списками русских названий. Второй
    список не менее важен первого: по нему видно, чем сделка была слаба, и
    именно он объясняет, почему две внешне одинаковые сделки разошлись.
    """
    if not factors:
        return [], []
    if isinstance(factors, (list, tuple, set)):
        return [factor(name) for name in factors], []
    ok = [factor(name) for name, state in factors.items() if state]
    missing = [factor(name) for name, state in factors.items() if not state]
    return ok, missing
