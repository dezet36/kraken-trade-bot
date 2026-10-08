"""
Рыночные данные для стратегий — дверь модуля «Анализ» (реорганизация, этап 4,
08.10.2026).

Стратегии берут данные только отсюда и из общего слоя анализа (структура
market_structure, режим market_regime, признаки потока flow_features, разметка
ИИ), а не из сборщиков и не с биржи напрямую. Сборщики — модуль «Данные»
(exchange, positioning, liquidations, flow_data, market_mood); стратегия о них не
знает. Сторож tests/test_module_layers.py проверяет, что модули стратегий не
импортируют данные мимо этой двери.

Вызовы идут через имена модулей сборщиков и в той же форме, что звали
стратегии до двери: незаданные аргументы не передаются. Модуль сборщика
берётся в момент вызова, а не при импорте двери: проверки перезагружают
сборщиков (positioning, exchange), и ссылка, взятая заранее, вела бы в старую
копию мимо подмены. Так проверки подменяют источник там же, где раньше
(positioning.settled_funding, market_mood.facts, fetch_ohlcv модуля
стратегии). Один снимок рынка на цикл держат кэши: свечи — на цикл (exchange,
90 с), структура — на закрытую свечу (market_structure).
"""


def _given(**kwargs):
    """Только заданные аргументы — форма вызова сборщика не меняется."""
    return {k: v for k, v in kwargs.items() if v is not None}


def fetch_ohlcv(timeframe, limit=500, symbol=None, client=None, since=None):
    """Свечи пары: последние limit (формирующаяся — последней), кэш на цикл."""
    import exchange
    return exchange.fetch_ohlcv(timeframe, limit=limit, symbol=symbol, client=client,
                                **_given(since=since))


def settled_funding(pair, client=None, now_ms=None):
    """Ставка фандинга, уже выплаченная к моменту решения (доля за 8 ч) или None."""
    import positioning
    return positioning.settled_funding(pair, client=client, **_given(now_ms=now_ms))


def latest(source, pair, upto=None, max_age_hours=None):
    """Последнее значение ряда позиционирования (фандинг, ОИ, доли) или None."""
    import positioning
    return positioning.latest(source, pair, **_given(upto=upto, max_age_hours=max_age_hours))


def series(source, pair, limit=None, upto=None):
    """Ряд позиционирования пары (записи сборщика)."""
    import positioning
    return positioning.series(source, pair, **_given(limit=limit, upto=upto))


def liquidation_rows(pair, since=None, upto=None):
    """Ликвидации пары за окно (поток Bybit)."""
    import liquidations
    return liquidations.rows(pair, **_given(since=since, upto=upto))


def flow_frame(pair, now_ms=None):
    """Часовая таблица потока пары (свечи, доля покупок, ОИ, фандинг)."""
    import flow_data
    return flow_data.frame(pair, now_ms)


def last_closed_hour(now_ms=None):
    """Метка открытия последнего закрытого часа (мс)."""
    import flow_data
    return flow_data.last_closed_hour(now_ms)


def mood(now_ms=None):
    """Настроение рынка на последний закрытый час (DVOL, премия Coinbase, доля спота)."""
    import market_mood
    return market_mood.facts(**_given(now_ms=now_ms))
