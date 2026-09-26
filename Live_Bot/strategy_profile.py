"""
Что у стратегии СВОЁ в исполнении: срок заявки, кулдаун, предел издержек,
смещение лимита, предел удержания позиции, снятие заявки у цели, исполнение
лимита, оказавшегося за рынком.

ЗАЧЕМ ОДНО МЕСТО. Правило проекта: общий параметр не имеет права запирать
одну стратегию. Пока ответ «своё или общее» лежал в трёх модулях
(paper_broker, exit_plan, trade_manager), он расходился: у SMC в params стоял
свой срок заявки 48 ч и свой кулдаун, а брокер читал 72 ч и 12 ч из config;
у Боллинджера кулдаун 2 ч в params — брокер брал 12; предел издержек 5%
из config резал SMC (доля 7–9% при её стопе 0.8–1.2%) и уровни (5.3–6.2%)
— 19–21.09.2026 SMC потеряла 9 сетапов, уровни — все три.

Здесь каждая величина берётся у стратегии, если она её объявила, и только
иначе — общая из config (а общая — это то, с чем считался Фибоначчи).
Исполнители зовут эти функции и не знают, откуда число.
"""



def _config():
    # Каждый раз через sys.modules: тесты перезагружают config между
    # проверками, и модуль, схваченный при импорте, был бы чужим.
    import config
    return config


def _levels():
    from levels import params
    return params


def _rsibb():
    from rsibb import params
    return params


def _smc():
    from smc import params
    return params


def _llm_rules():
    """Правила ИИ, если он в режиме «правила» (config.LLM_MODE=rules), иначе None."""
    import llm_rules
    return llm_rules.DECISION if llm_rules.enabled() else None


def _bar_hours(timeframe):
    return {'1m': 1 / 60, '5m': 1 / 12, '15m': 0.25, '30m': 0.5,
            '1h': 1.0, '2h': 2.0, '4h': 4.0, '1d': 24.0}.get(str(timeframe), 1.0)


def _own(strategy, getter, fallback):
    """Значение стратегии или запасное, если у неё нет или оно сломано."""
    try:
        value = getter(strategy)
        if value is not None:
            return float(value)
    except Exception:                              # noqa: BLE001
        pass
    return float(fallback)


def expiry_hours(strategy):
    """Сколько живёт неналитая заявка."""
    def own(name):
        if name == 'LEVELS':
            return _levels().EXPIRY_HOURS
        if name == 'RSIBB':
            return _rsibb().EXPIRY_BARS * _bar_hours(_rsibb().TIMEFRAME)
        if name == 'SMC':
            return _smc().PENDING_ORDER_MAX_HOURS
        if name == 'LLM':
            if _llm_rules() is not None:
                return _llm_rules().PENDING_ORDER_MAX_HOURS
            # Заявка — часть плана и живёт столько же, сколько ждёт условия план.
            return getattr(_config(), 'LLM_TRIGGER_TTL_H', 12) or 12
        return None
    return _own(strategy, own, getattr(_config(), 'PENDING_ORDER_MAX_HOURS', 72.0))


def cooldown_hours(strategy):
    """Пауза по паре после выхода."""
    def own(name):
        if name == 'LEVELS':
            return _levels().COOLDOWN_HOURS
        if name == 'RSIBB':
            return _rsibb().COOLDOWN_HOURS
        if name == 'SMC':
            return _smc().COOLDOWN_HOURS
        if name == 'LLM':
            if _llm_rules() is not None:
                return _llm_rules().COOLDOWN_HOURS
            return getattr(_config(), 'LLM_COOLDOWN_HOURS', 4.0)
        return None
    return _own(strategy, own, getattr(_config(), 'COOLDOWN_HOURS', 12.0))


def cost_limit_pct(strategy):
    """Какую долю риска позволено съесть комиссиям за вход и выход."""
    def own(name):
        if name == 'LEVELS':
            return _levels().MAX_ENTRY_COST_SHARE_PCT
        if name == 'RSIBB':
            return _rsibb().MAX_ENTRY_COST_SHARE_PCT
        if name == 'SMC':
            return _smc().MAX_ENTRY_COST_SHARE_PCT
        if name == 'LLM':
            if _llm_rules() is not None:
                return _llm_rules().MAX_ENTRY_COST_SHARE_PCT
            return getattr(_config(), 'LLM_MAX_ENTRY_COST_SHARE_PCT', 5.0)
        return None
    return _own(strategy, own, getattr(_config(), 'MAX_ENTRY_COST_SHARE_PCT', 5.0))


def limit_offset_pct(strategy):
    """
    Смещение лимита от расчётного входа (доля, 0.001 = 0.1%).

    У ИИ — ноль: вход — точный уровень из плана, и сдвиг делал бы его на
    0.1% хуже, чем спланировала модель. Остальные — как в замерах (общее).
    """
    if not getattr(_config(), 'USE_LIMIT_ENTRY', True):
        return 0.0
    if strategy == 'LLM':
        return float(getattr(_config(), 'LLM_LIMIT_ENTRY_OFFSET_PCT', 0.0))
    return float(getattr(_config(), 'LIMIT_ENTRY_OFFSET_PCT', 0.0))


def max_hold_hours(strategy):
    """Предел удержания позиции; 0 — без предела."""
    def own(name):
        if name == 'LEVELS':
            return _levels().MAX_HOLD_HOURS
        if name == 'RSIBB':
            return _rsibb().MAX_HOLD_BARS * _bar_hours(_rsibb().TIMEFRAME)
        if name == 'SMC':
            return _smc().MAX_POSITION_HOLD_HOURS
        if name == 'LLM':
            if _llm_rules() is not None:
                return _llm_rules().MAX_POSITION_HOLD_HOURS
            return getattr(_config(), 'LLM_MAX_HOLD_HOURS', 336.0)
        return None
    return _own(strategy, own, getattr(_config(), 'MAX_POSITION_HOLD_HOURS', 0.0) or 0.0)


def drops_at_target(strategy):
    """
    Снимать ли неналитую заявку, когда цена дошла до первой цели, не задев
    входа («цена дошла до цели без нас»).

    Общее — снимать: так брокер жил с первого дня. У SMC — нет: её бэктест
    этого правила не знал, и замер 25.09.2026 показал, что оно отнимает у неё
    лучшие сделки (smc/params.CANCEL_PENDING_AT_TARGET).
    """
    try:
        if strategy == 'SMC':
            return bool(_smc().CANCEL_PENDING_AT_TARGET)
        if strategy == 'LLM' and _llm_rules() is not None:
            return bool(_llm_rules().CANCEL_PENDING_AT_TARGET)
    except Exception:                              # noqa: BLE001
        pass
    return bool(getattr(_config(), 'CANCEL_PENDING_AT_TARGET', True))


def fills_through_market(strategy):
    """
    Лимит, который при постановке уже стоит ПО ТУ СТОРОНУ рынка (покупка не
    ниже цены, продажа не выше), исполняется сразу и по рынку, с комиссией
    тейкера, — как на бирже. Выключено — брокер ставит такой лимит ждать и
    наливает по цене лимита на следующей свече: на бумаге вход хуже рынка
    на весь зазор.

    ОБЩЕГО ЗНАЧЕНИЯ НЕТ: каждая стратегия объявляет своё, незнакомая —
    выключено, как брокер жил всегда. Правило меняет исполнение, а исполнение
    обязано совпадать с замером стратегии. У ИИ включено по разбору
    26.09.2026: DOGE и XLM налиты по лимиту на 0.8–1% хуже рынка, у восьми
    закрытых сделок переплата 1.63R. Остальным — только после замера на своём
    движке: у Боллинджера заявка стоит на полосе или на закрытии бара, то
    есть часто у самой цены, и правило перевело бы его на входы тейкером.
    """
    try:
        if strategy == 'LEVELS':
            return bool(_levels().FILL_THROUGH_MARKET)
        if strategy == 'RSIBB':
            return bool(_rsibb().FILL_THROUGH_MARKET)
        if strategy == 'SMC':
            return bool(_smc().FILL_THROUGH_MARKET)
        if strategy == 'LLM':
            if _llm_rules() is not None:
                return bool(_llm_rules().FILL_THROUGH_MARKET)
            return bool(getattr(_config(), 'LLM_FILL_THROUGH_MARKET', True))
        if strategy == 'FIBO':
            return bool(getattr(_config(), 'FIBO_FILL_THROUGH_MARKET', False))
    except Exception:                              # noqa: BLE001
        pass
    return False


# Кто читает ручку оператора «минимальный стоп» (settings_store). Уровни и
# Боллинджер считают по своему params.MIN_STOP_PCT, ИИ — по издержкам; для
# них ручка мертва, и панель обязана это показывать, а не рисовать 0.8%.
MIN_STOP_KNOB_READERS = ('FIBO', 'SMC')


def min_stop_pct(strategy):
    """
    Минимальная дистанция стопа, % от цены, которой стратегия РЕАЛЬНО живёт.

    FIBO и SMC берут ручку оператора (у SMC адаптер пишет её в MIN_SL_PCT
    перед каждым разбором); уровни и Боллинджер — свой параметр; ИИ — вывод
    из своего предела издержек и ATR (llm_context.min_stop_pct).
    """
    try:
        if strategy == 'LEVELS':
            return float(_levels().MIN_STOP_PCT)
        if strategy == 'RSIBB':
            return float(_rsibb().MIN_STOP_PCT)
        if strategy == 'LLM':
            if _llm_rules() is not None:
                return float(_llm_rules().MIN_SL_PCT) * 100
            import llm_context
            return float(llm_context.min_stop_pct())
        import settings_store
        return float(settings_store.min_stop_pct(strategy)) * 100
    except Exception:                              # noqa: BLE001
        return float(getattr(_config(), 'MIN_SL_PERCENT', 0.008)) * 100


def describe(strategy):
    """Все величины разом — для журнала при старте и для панели."""
    return {
        'expiry_hours': expiry_hours(strategy),
        'cooldown_hours': cooldown_hours(strategy),
        'cost_limit_pct': cost_limit_pct(strategy),
        'limit_offset_pct': limit_offset_pct(strategy),
        'max_hold_hours': max_hold_hours(strategy),
        'min_stop_pct': min_stop_pct(strategy),
        'min_stop_knob': strategy in MIN_STOP_KNOB_READERS,
        'drops_at_target': drops_at_target(strategy),
        'fills_through_market': fills_through_market(strategy),
    }
