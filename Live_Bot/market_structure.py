"""
Живой структурный контекст пары — ОБЩИЙ СЛОЙ, а не часть стратегии SMC.

Свечи с биржи → закрытые свечи трёх таймфреймов → MarketContext (свинги,
сломы, зоны, имбалансы, пулы ликвидности). Считается один раз на новую
закрытую свечу рабочего ТФ и отдаётся всем, кто читает структуру: SMC
принимает по нему решения у себя, ИИ описывает его в разметке, панель
рисует. Правило проекта (CLAUDE.md): структурные элементы находятся и
считаются в общем слое, стратегии — потребители.

До 21.09.2026 это лежало в strategy_smc — адаптере стратегии SMC, — и ИИ
импортировал стратегию SMC, чтобы получить структуру. Формально это
работало, но нарушало правило: правка адаптера SMC могла изменить то, что
видит модель. Теперь адаптер и ИИ читают одно и то же место, а
strategy_smc держит лишь псевдонимы для старого кода.

Модуль лежит рядом с market_regime.py, а не внутри smc/: пакет smc/ —
чистая математика без биржи (это держит tests/test_strategy_isolation), а
здесь единственная связь с биржей — fetch_ohlcv, и зовётся она через имя
модуля, чтобы тесты подменяли источник свечей, не трогая биржу.
"""

from exchange import fetch_ohlcv
from logger import log
from smc import params
from smc import signal as smc_signal

# pair -> (последний timestamp закрытой свечи рабочего ТФ, контекст)
_cache = {}


def drop_forming_candle(df):
    """
    Убирает последнюю (ещё не закрытую) свечу.

    ccxt отдаёт последней формирующуюся свечу; её high/low меняются каждую
    секунду, и свинги с зонами по ней «прыгали» бы. Структура — по закрытым.
    """
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


def load_frames(pair, client=None):
    """
    Закрытые свечи трёх таймфреймов: 1D задаёт bias, 4H уточняет структуру
    старшего порядка, 1H — рабочий ТФ поиска зон. Раскладка — параметры
    СТРУКТУРЫ (smc/params, секция структуры), общие для всех читателей.
    """
    frames = {}
    for key, timeframe, limit in (
        ('bias', params.TF_BIAS, params.LOOKBACK_BIAS),
        ('htf', params.TF_HTF, params.LOOKBACK_HTF),
        ('poi', params.TF_POI, params.LOOKBACK_POI),
    ):
        raw = fetch_ohlcv(timeframe, limit=limit + 5, symbol=pair, client=client)
        closed = drop_forming_candle(raw)
        # Достаточность истории проверяет ЯДРО на момент решения
        # (params.MIN_HTF_BARS в bias_at). Здесь только отсутствие данных:
        # длина фрейма — не то же самое, что число закрытых свечей на свече
        # решения, и дублировать правило по длине значит раздвоить его.
        if closed is None or len(closed) < 2:
            if key == 'poi':
                return None
            # Пропуск старшего ТФ не молчаливый: без него направление
            # старшего порядка не определить, и ядро вернёт NEUTRAL. Знать об
            # этом надо — иначе пара просто «не торгуется» без объяснимой
            # причины.
            log(f"   {pair}: нет данных {timeframe} "
                f"({len(closed) if closed is not None else 0} свечей) — "
                f"направление старшего порядка определить нечем")
            continue
        frames[key] = closed
    return frames if 'poi' in frames else None


def cached(pair):
    """Контекст из кэша без обращения к бирже. None — ещё не строился."""
    entry = _cache.get(pair)
    return entry[1] if entry else None


def get(pair, client=None, force=False):
    """
    Контекст пары, пересобираемый только при появлении новой закрытой свечи
    рабочего ТФ. Построение структуры — тяжёлая операция, а бот сканирует
    пул каждые пять минут при часовом рабочем ТФ.
    """
    frames = load_frames(pair, client=client)
    if frames is None:
        return None

    last_ts = frames['poi']['timestamp'].iloc[-1]
    entry = _cache.get(pair)
    if entry and not force and entry[0] == last_ts:
        return entry[1]

    context = smc_signal.build_context(frames, pair=pair)
    _cache[pair] = (last_ts, context)
    return context


def clear():
    """Сбросить кэш (тесты, смена биржи)."""
    _cache.clear()
