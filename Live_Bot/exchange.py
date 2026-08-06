import ccxt
import pandas as pd
import config
from logger import log

_exchange_instance = None       # legacy single-user клиент (из .env)
_market_client = None           # общий keyless клиент для market-data (сканер)

SUPPORTED_EXCHANGES = ('bybit', 'bingx')


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
        # BingX: отдельного demo endpoint в ccxt нет — торговля на реальном счёте
    else:
        raise ValueError(f"Биржа не поддерживается: {exchange_name}. Доступно: {SUPPORTED_EXCHANGES}")
    return client


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

    if config.EXCHANGE_NAME.lower() == 'bybit':
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
        log("🔴 Подключение к BingX LIVE (РЕАЛЬНЫЙ СЧЁТ)...")
        if config.TRADING_MODE == 'DEMO':
            log("⚠️  BingX не имеет demo endpoint — используется реальный счёт")

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
        ohlcv = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
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
