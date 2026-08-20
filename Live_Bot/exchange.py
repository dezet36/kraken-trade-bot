import ccxt
import pandas as pd
import config
from logger import log

_exchange_instance = None       # legacy single-user клиент (из .env)
_market_client = None           # общий keyless клиент для market-data (сканер)

SUPPORTED_EXCHANGES = ('bybit', 'bingx')

# ── Символы ──────────────────────────────────────────────────────────────────
# Пул задан биржевыми символами Bybit ('BTCUSDT'). BingX называет тот же рынок
# иначе ('BTC-USDT') и на 'BTCUSDT' отвечает BadSymbol — проверено запросом.
# Общая для обеих запись — единый символ ccxt ('BTC/USDT:USDT'), и приведение к
# нему делается ровно на границе с биржей.
#
# Без этого вторая биржа не работает вообще: ни свечи, ни ордера. Ошибка при
# этом не тихая, а громкая, что редкая удача, — но чинить её надо один раз в
# одном месте, а не в каждом вызове.
_symbol_cache = {}          # (id биржи, наш символ) -> символ для этой биржи


def market_symbol(pair, client):
    """
    Наш символ в записи КОНКРЕТНОЙ биржи.

    Сопоставление идёт по биржевому id рынка, а не по разбору строки: 'SHIB1000'
    у одной биржи и '1000SHIB' у другой — не то, что можно надёжно вывести
    правилом. Если рынка нет, возвращается None, и вызывающая сторона обязана
    это учесть: у BingX нет одной из наших пар.
    """
    if client is None:
        return pair
    key = (getattr(client, 'id', '?'), pair)
    if key in _symbol_cache:
        return _symbol_cache[key]

    resolved = None
    try:
        if not client.markets:
            client.load_markets()
        # СНАЧАЛА БЕССРОЧНЫЕ КОНТРАКТЫ, И ЭТО НЕ ПРЕДПОЧТЕНИЕ, А ИСПРАВЛЕНИЕ.
        # У Bybit спотовый рынок 'XRP/USDT' и бессрочный 'XRP/USDT:USDT' имеют
        # ОДИН И ТОТ ЖЕ биржевой id 'XRPUSDT'. Поиск по id без разбора брал
        # первый попавшийся, и для части пар это оказывался спот — а у спота
        # нет ни фандинга, ни открытого интереса, и биржа отвечала «символ не
        # поддерживает этот тип рынка». Проявлялось выборочно, по одним парам
        # и не по другим, то есть выглядело как сбой связи.
        #
        # Бот торгует исключительно бессрочные, поэтому спот здесь не запасной
        # вариант, а источник ошибок.
        def _rank(market):
            return (bool(market.get('swap')), bool(market.get('linear')))

        matches = [(symbol, market) for symbol, market in client.markets.items()
                   if market.get('id') == pair]
        matches.sort(key=lambda item: _rank(item[1]), reverse=True)
        for symbol, market in matches:
            if market.get('swap'):
                resolved = symbol
                break
        # 2. Иначе собираем единый символ из базовой валюты — бессрочный первым.
        if resolved is None and pair.endswith('USDT'):
            base = pair[:-4]
            for candidate in (f'{base}/USDT:USDT', f'{base}/USDT'):
                if candidate in client.markets:
                    resolved = candidate
                    break
        # 3. Последняя попытка: совпадение по id вообще, каким бы ни был тип.
        if resolved is None and matches:
            resolved = matches[0][0]
    except Exception as exc:                       # noqa: BLE001
        log(f'⚠️ не удалось сопоставить {pair}: {exc}')
    _symbol_cache[key] = resolved
    return resolved


def available_pairs(pairs, client):
    """
    Какие из наших пар биржа вообще знает.

    У BingX из 21 пары пула нет одной. Молча отправлять её в запросы значило бы
    получать ошибку на каждом цикле по паре, которой там просто не существует.
    """
    return [pair for pair in pairs if market_symbol(pair, client)]


# ── Возможности бирж ─────────────────────────────────────────────────────────
# Не все биржи отдают одно и то же, и различия существенные. Замерено запросом:
#
#     возможность                    bybit   bingx
#     открытый интерес, история        да     нет
#     соотношение лонг/шорт            да     нет
#     премия над индексом              да     нет
#     фандинг, история                 да      да
#
# Сборщик позиционирования обязан это учитывать: иначе на BingX он каждый час
# писал бы в журнал отказы по трём источникам из четырёх.
CAPABILITIES = ('fetchOpenInterestHistory', 'fetchLongShortRatioHistory',
                'fetchPremiumIndexOHLCV', 'fetchFundingRateHistory')


def supports(client, capability):
    """
    Отдаёт ли эта биржа такие данные.

    При невозможности выяснить отвечаем ДА, а не НЕТ. Разница принципиальная:
    «биржа не умеет» — это её свойство, о котором молчат, а «не удалось
    спросить» — это сбой, который обязан проявиться. Осторожный ответ «нет»
    привёл бы к тому, что упавшая биржа тихо отключала бы весь сбор данных, и
    в журнале не осталось бы ни строчки. Отвечая «да», мы даём запросу
    состояться и настоящей ошибке дойти до журнала.
    """
    try:
        return bool(client.has.get(capability))
    except Exception:                              # noqa: BLE001
        return True


# ── Хелперы постройки клиента ────────────────────────────────────────────────
def _apply_bybit_demo(client):
    """Переключает Bybit-клиент на Demo Trading endpoint (api-demo.bybit.com)."""
    for key in list(client.urls['api'].keys()):
        client.urls['api'][key] = 'https://api-demo.bybit.com'
    # /v5/asset/coin/query-info не поддерживается в demo — патчим
    client.fetch_currencies = lambda params={}: {}
    client.currencies = {}
    client.currencies_by_id = {}


def make_client(exchange_name: str, api_key: str, api_secret: str, mode: str = 'DEMO'):
    """
    Фабрика: ccxt-клиент под конкретные ключи (мульти-тенант).
    exchange_name: 'bybit' | 'bingx'; mode: 'DEMO' | 'LIVE'.
    """
    name = (exchange_name or 'bybit').lower()
    if name == 'bybit':
        client = ccxt.bybit({
            'apiKey': api_key, 'secret': api_secret,
            'enableRateLimit': True, 'options': {'defaultType': 'linear'},
            'timeout': 30000,
        })
        if mode == 'DEMO':
            _apply_bybit_demo(client)
    elif name == 'bingx':
        client = ccxt.bingx({
            'apiKey': api_key, 'secret': api_secret,
            'enableRateLimit': True, 'options': {'defaultType': 'swap'},
            'timeout': 30000,
        })
        if mode == 'DEMO':
            # ДЕМО-КОНТУР У BINGX ЕСТЬ. Здесь стояло «отдельного demo endpoint
            # в ccxt нет — торговля на реальном счёте», и это было верно
            # когда-то, а потом устарело молча. Последствие тяжёлое: человек
            # выбирал DEMO, панель показывала DEMO, подпись каждой сделки была
            # зелёной — и торговали настоящие деньги.
            #
            # ccxt 4.5 отдаёт для bingx контур open-api-vst (VST — виртуальные
            # средства) через штатный set_sandbox_mode.
            client.set_sandbox_mode(True)
    else:
        raise ValueError(f"Биржа не поддерживается: {exchange_name}. Доступно: {SUPPORTED_EXCHANGES}")
    return client


def effective_mode(client, requested):
    """
    Какой режим ПОЛУЧИЛСЯ, а не какой просили.

    Подпись режима бралась из config.TRADING_MODE — то есть из намерения, а не
    из результата. Пока у BingX не включался демо-контур, каждая сделка
    помечалась «🟢 DEMO» на реальном счёте. Настройка и факт обязаны быть
    разными величинами, иначе несоответствие между ними непредставимо.

    Признак — адрес, по которому клиент реально ходит.
    """
    if str(requested).upper() != 'DEMO':
        return 'LIVE'
    try:
        urls = client.urls.get('api')
        text = ' '.join(urls.values()) if isinstance(urls, dict) else str(urls)
    except Exception:                              # noqa: BLE001
        return 'LIVE'                              # не смогли убедиться — не обещаем
    demo = ('api-demo.bybit' in text or 'testnet' in text
            or 'open-api-vst' in text)
    return 'DEMO' if demo else 'LIVE'


def make_market_client(exchange_name: str = 'bybit'):
    """Keyless клиент для публичных market-data (OHLCV) — общий на весь скан, LIVE endpoint."""
    global _market_client
    if _market_client is not None:
        return _market_client
    name = (exchange_name or 'bybit').lower()
    if name == 'bingx':
        _market_client = ccxt.bingx({'enableRateLimit': True, 'options': {'defaultType': 'swap'}, 'timeout': 30000})
    else:
        _market_client = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'linear'}, 'timeout': 30000})
    return _market_client


def validate_credentials(exchange_name: str, api_key: str, api_secret: str, mode: str = 'DEMO'):
    """
    Проверяет ключи при онбординге. Возвращает (ok: bool, balance_usdt: float, error: str|None).
    Прим.: программно проверить «нет права вывода» по биржам ненадёжно — пользователю
    отдельно показываем требование создавать trade-only ключ.
    """
    try:
        client = make_client(exchange_name, api_key, api_secret, mode)
        client.load_markets()
        bal = client.fetch_balance()
        usdt = bal.get('USDT', {}).get('total', 0) or 0
        return True, float(usdt), None
    except ccxt.AuthenticationError:
        return False, 0.0, "Неверный API-ключ или секрет (ошибка аутентификации)"
    except ccxt.PermissionDenied as e:
        return False, 0.0, f"Недостаточно прав у ключа: {e}"
    except Exception as e:
        return False, 0.0, str(e)


# ── Legacy single-user (используется текущим ботом до перехода на платформу) ──
def configured_exchanges():
    """
    У каких бирж есть ключи. Переключаться можно только на настроенную.

    Ключи живут в .env и НЕ принимаются через дашборд сознательно: у него нет
    пароля (о чём отдельно написано в списке дел перед реальным счётом), и
    приём секретов по открытому HTTP был бы дырой, а не удобством.
    """
    out = {}
    out['bybit'] = bool(config.BYBIT_API_KEY and config.BYBIT_SECRET_KEY)
    out['bingx'] = bool(config.BINGX_API_KEY and config.BINGX_SECRET_KEY)
    return out


def active_exchange_name():
    """
    Какая биржа выбрана сейчас: настройка с панели, иначе .env.

    Настройка перекрывает .env, но только если у выбранной биржи есть ключи:
    иначе бот при старте упал бы на выборе, сделанном когда-то мышкой.
    """
    name = (config.EXCHANGE_NAME or 'bybit').lower()
    try:
        import settings_store
        chosen = (settings_store.exchange_name() or '').lower()
        if chosen in SUPPORTED_EXCHANGES and configured_exchanges().get(chosen):
            name = chosen
    except Exception:                              # noqa: BLE001
        pass
    return name


def get_exchange():
    """Кешированное подключение из .env (legacy, одно-юзерный режим)."""
    global _exchange_instance
    if _exchange_instance is not None:
        return _exchange_instance

    # PAPER подключается к тому же эндпоинту, что и DEMO. Фантому нужны только
    # котировки, счёт он ведёт у себя; заходить при этом на боевой эндпоинт
    # незачем и опасно. Раньше сравнение mode == 'DEMO' было ложным для PAPER,
    # и фантом открывал боевого клиента: с демо-ключами авторизованные вызовы
    # падали, а с боевыми — читал реальный счёт, чего от фантома никто не ждёт.
    endpoint_mode = 'DEMO' if config.TRADING_MODE in ('DEMO', 'PAPER') else 'LIVE'

    if active_exchange_name() == 'bybit':
        if not config.BYBIT_API_KEY or not config.BYBIT_SECRET_KEY:
            raise Exception("BYBIT_API_KEY или BYBIT_SECRET_KEY не загружены из .env!")
        _exchange_instance = make_client('bybit', config.BYBIT_API_KEY, config.BYBIT_SECRET_KEY,
                                         endpoint_mode)
        if endpoint_mode == 'DEMO':
            label = 'ФАНТОМ' if config.PAPER_MODE else 'DEMO'
            log(f"🟢 Подключение к Bybit {label} (api-demo.bybit.com)...")
        else:
            log("🔴 Подключение к Bybit LIVE (РЕАЛЬНЫЙ СЧЁТ)...")
    else:
        if not config.BINGX_API_KEY or not config.BINGX_SECRET_KEY:
            raise Exception("BINGX_API_KEY или BINGX_SECRET_KEY не загружены из .env!")
        _exchange_instance = make_client('bingx', config.BINGX_API_KEY, config.BINGX_SECRET_KEY,
                                         endpoint_mode)
        # Говорим то, что ПОЛУЧИЛОСЬ. Прежде здесь безусловно писалось «LIVE
        # (РЕАЛЬНЫЙ СЧЁТ)» — и это было правдой, но подпись самих сделок при
        # этом брала режим из настройки и оставалась зелёной.
        if effective_mode(_exchange_instance, endpoint_mode) == 'DEMO':
            label = 'ФАНТОМ' if config.PAPER_MODE else 'DEMO'
            log(f"🟢 Подключение к BingX {label} (open-api-vst.bingx.com)...")
        else:
            log("🔴 Подключение к BingX LIVE (РЕАЛЬНЫЙ СЧЁТ)...")

    return _exchange_instance


def reset_exchange():
    """Сбрасывает кеш подключения (используется при сетевых ошибках)"""
    global _exchange_instance
    _exchange_instance = None


def fetch_ohlcv(timeframe, limit=500, symbol=None, client=None, since=None):
    """
    Загружает свечи. client=None -> legacy get_exchange() (одно-юзер).
    Для платформы передавай общий market-client (make_market_client).

    since — отметка начала в миллисекундах. Без неё биржа отдаёт ПОСЛЕДНИЕ
    limit свечей от текущего момента, и запросить прошлое нельзя в принципе.
    Именно поэтому у закрытых сделок не строился график: окно сделки лежало
    раньше отданного куска, фильтр по времени не находил ни одной свечи, и
    дашборд честно отвечал «свечей за этот период нет».
    """
    if symbol is None:
        symbol = config.TRADING_PAIRS[0]
    try:
        ex = client if client is not None else get_exchange()
        # Приведение к записи ЭТОЙ биржи. Без него BingX отвечает BadSymbol на
        # каждый запрос: наш пул записан символами Bybit.
        native = market_symbol(symbol, ex)
        if native is None:
            log(f'⚠️ {symbol}: нет такого рынка на {getattr(ex, "id", "бирже")}')
            return None
        ohlcv = ex.fetch_ohlcv(native, timeframe, since=since, limit=limit)
        if not ohlcv or len(ohlcv) < 10:
            log(f"⚠️ Мало данных для {symbol} {timeframe}: {len(ohlcv) if ohlcv else 0} свечей")
            return None
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except ccxt.NetworkError as e:
        log(f"⚠️ Сетевая ошибка ({symbol} {timeframe}): {e}")
        if client is None:
            reset_exchange()
        return None
    except Exception as e:
        log(f"❌ Ошибка загрузки свечей ({symbol} {timeframe}): {e}")
        return None


def test_connection():
    """Проверяет legacy-подключение из .env и выводит баланс"""
    try:
        log("🔄 Тестирую подключение...")
        exchange = get_exchange()
        log("📋 Загружаю информацию о рынках...")
        exchange.load_markets()
        log("💰 Запрашиваю баланс...")
        balance = exchange.fetch_balance()
        usdt_balance = balance.get('USDT', {}).get('total', 0) or 0
        mode_label = "DEMO (api-demo.bybit.com)" if config.TRADING_MODE == 'DEMO' else "⚠️ LIVE — РЕАЛЬНЫЕ ДЕНЬГИ"
        log(f"✅ Подключение к {config.EXCHANGE_NAME.upper()} успешно!")
        log(f"   Режим: {mode_label}")
        log(f"   Баланс USDT: ${usdt_balance:.2f}")
        log(f"   Торговых пар: {len(config.TRADING_PAIRS)}")
        return True
    except ccxt.AuthenticationError:
        log("❌ Ошибка аутентификации! Проверь API Key и Secret Key в .env")
        return False
    except Exception as e:
        log(f"❌ Ошибка подключения: {e}")
        return False


if __name__ == "__main__":
    test_connection()
