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

С 08.10.2026 «своё» каждая стратегия отдаёт сама — функцией profile()
своего адаптера (strategies/registry.py); здесь — общие запасные значения
и одна точка входа для исполнителей. Ветки по именам стратегий ушли.
"""



def _config():
    # Каждый раз через sys.modules: тесты перезагружают config между
    # проверками, и модуль, схваченный при импорте, был бы чужим.
    import config
    return config


def _bar_hours(timeframe):
    """Часов в баре таймфрейма (для сроков, объявленных в барах)."""
    return {'1m': 1 / 60, '5m': 1 / 12, '15m': 0.25, '30m': 0.5,
            '1h': 1.0, '2h': 2.0, '4h': 4.0, '1d': 24.0}.get(str(timeframe), 1.0)


def _profile(strategy):
    """Величины, объявленные стратегией (адаптер.profile()); {} — если нет."""
    from strategies import registry
    if registry.get(strategy) is None:
        return {}
    return registry.adapter(strategy).profile() or {}


def _own(strategy, key, fallback):
    """Значение стратегии или запасное, если у неё нет или оно сломано."""
    try:
        value = _profile(strategy).get(key)
        if value is not None:
            return float(value)
    except Exception:                              # noqa: BLE001
        pass
    return float(fallback)


def _own_flag(strategy, key, fallback):
    try:
        value = _profile(strategy).get(key)
        if value is not None:
            return bool(value)
    except Exception:                              # noqa: BLE001
        pass
    return bool(fallback)


def expiry_hours(strategy):
    """Сколько живёт неналитая заявка."""
    return _own(strategy, 'expiry_hours', getattr(_config(), 'PENDING_ORDER_MAX_HOURS', 72.0))


def cooldown_hours(strategy):
    """Пауза по паре после выхода."""
    return _own(strategy, 'cooldown_hours', getattr(_config(), 'COOLDOWN_HOURS', 12.0))


def cost_limit_pct(strategy):
    """Какую долю риска позволено съесть комиссиям за вход и выход."""
    return _own(strategy, 'cost_limit_pct', getattr(_config(), 'MAX_ENTRY_COST_SHARE_PCT', 5.0))


def limit_offset_pct(strategy):
    """
    Смещение лимита от расчётного входа (доля, 0.001 = 0.1%).

    У ИИ — ноль: вход — точный уровень из плана, и сдвиг делал бы его на
    0.1% хуже, чем спланировала модель. У Фибо 12ч — ноль: замер ставил лимит
    ровно на уровень. Остальные — как в замерах (общее).
    """
    if not getattr(_config(), 'USE_LIMIT_ENTRY', True):
        return 0.0
    return _own(strategy, 'limit_offset_pct', getattr(_config(), 'LIMIT_ENTRY_OFFSET_PCT', 0.0))


def max_hold_hours(strategy):
    """Предел удержания позиции; 0 — без предела."""
    return _own(strategy, 'max_hold_hours', getattr(_config(), 'MAX_POSITION_HOLD_HOURS', 0.0) or 0.0)


def drops_at_target(strategy):
    """
    Снимать ли неналитую заявку, когда цена дошла до первой цели, не задев
    входа («цена дошла до цели без нас»).

    Общее — снимать: так брокер жил с первого дня. У SMC — нет: её бэктест
    этого правила не знал, и замер 25.09.2026 показал, что оно отнимает у неё
    лучшие сделки (smc/params.CANCEL_PENDING_AT_TARGET).
    """
    return _own_flag(strategy, 'drops_at_target', getattr(_config(), 'CANCEL_PENDING_AT_TARGET', True))


def fills_through_market(strategy):
    """
    Лимит, который при постановке уже стоит ПО ТУ СТОРОНУ рынка (покупка не
    ниже цены, продажа не выше), исполняется сразу и по рынку, с комиссией
    тейкера, — как на бирже. Выключено — брокер ставит такой лимит ждать и
    наливает по цене лимита на следующей свече: на бумаге вход хуже рынка
    на весь зазор.

    ОБЩЕГО ЗНАЧЕНИЯ НЕТ: каждая стратегия объявляет своё, незнакомая —
    выключено, как брокер жил всегда. Правило меняет исполнение, а исполнение
    обязано совпадать с замером стратегии.
    """
    return _own_flag(strategy, 'fills_through_market', False)


# Кто читает ручку оператора «минимальный стоп» (settings_store). Уровни,
# Боллинджер, SMCS, Фибо 12ч считают по своему params.MIN_STOP_PCT, ИИ — по
# издержкам; для них ручка мертва, и панель обязана это показывать, а не
# рисовать 0.8%.
MIN_STOP_KNOB_READERS = ('FIBO', 'SMC')


def min_stop_pct(strategy):
    """
    Минимальная дистанция стопа, % от цены, которой стратегия РЕАЛЬНО живёт.

    FIBO и SMC берут ручку оператора (у SMC адаптер пишет её в MIN_SL_PCT
    перед каждым разбором); остальные объявляют своё в profile().
    """
    try:
        own = _profile(strategy).get('min_stop_pct')
        if own is not None:
            return float(own)
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
