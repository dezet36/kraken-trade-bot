"""
Что у стратегии СВОЁ в исполнении: срок заявки, кулдаун, предел издержек,
смещение лимита, предел удержания позиции.

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
            return getattr(_config(), 'LLM_MAX_HOLD_HOURS', 336.0)
        return None
    return _own(strategy, own, getattr(_config(), 'MAX_POSITION_HOLD_HOURS', 0.0) or 0.0)


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
    }
