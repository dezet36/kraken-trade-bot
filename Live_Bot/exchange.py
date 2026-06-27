import ccxt
import pandas as pd
import config
from logger import log

_exchange_instance = None


def get_exchange():
    """Возвращает кешированное подключение к бирже"""
    global _exchange_instance
    if _exchange_instance is not None:
        return _exchange_instance

    if config.EXCHANGE_NAME.lower() == 'bybit':
        if not config.BYBIT_API_KEY or not config.BYBIT_SECRET_KEY:
            raise Exception("BYBIT_API_KEY или BYBIT_SECRET_KEY не загружены из .env!")

        params = {
            'apiKey': config.BYBIT_API_KEY,
            'secret': config.BYBIT_SECRET_KEY,
            'enableRateLimit': True,
            'options': {'defaultType': 'linear'},
            'timeout': 30000,
        }
        _exchange_instance = ccxt.bybit(params)

        if config.TRADING_MODE == 'DEMO':
            # Bybit Demo Trading: отдельный endpoint, НЕ sandbox/testnet
            # https://bybit-exchange.github.io/docs/v5/demo
            for key in list(_exchange_instance.urls['api'].keys()):
                _exchange_instance.urls['api'][key] = 'https://api-demo.bybit.com'
            # /v5/asset/coin/query-info не поддерживается в demo — патчим
            _exchange_instance.fetch_currencies = lambda params={}: {}
            _exchange_instance.currencies = {}
            _exchange_instance.currencies_by_id = {}
            log("🟢 Подключение к Bybit DEMO (api-demo.bybit.com)...")
        else:
            log("🔴 Подключение к Bybit LIVE (РЕАЛЬНЫЙ СЧЁТ)...")

    else:
        if not config.BINGX_API_KEY or not config.BINGX_SECRET_KEY:
            raise Exception("BINGX_API_KEY или BINGX_SECRET_KEY не загружены из .env!")

        params = {
            'apiKey': config.BINGX_API_KEY,
            'secret': config.BINGX_SECRET_KEY,
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'},
            'timeout': 30000,
        }
        _exchange_instance = ccxt.bingx(params)
        log("🔴 Подключение к BingX LIVE (РЕАЛЬНЫЙ СЧЁТ)...")
        if config.TRADING_MODE == 'DEMO':
            log("⚠️  BingX не имеет demo endpoint — используется реальный счёт")

    return _exchange_instance


def reset_exchange():
    """Сбрасывает кеш подключения (используется при сетевых ошибках)"""
    global _exchange_instance
    _exchange_instance = None


def fetch_ohlcv(timeframe, limit=500, symbol=None):
    """Загружает свечи с биржи"""
    if symbol is None:
        symbol = config.TRADING_PAIRS[0]

    try:
        exchange = get_exchange()
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

        if not ohlcv or len(ohlcv) < 10:
            log(f"⚠️ Мало данных для {symbol} {timeframe}: {len(ohlcv) if ohlcv else 0} свечей")
            return None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

    except ccxt.NetworkError as e:
        log(f"⚠️ Сетевая ошибка ({symbol} {timeframe}): {e}")
        reset_exchange()
        return None
    except Exception as e:
        log(f"❌ Ошибка загрузки свечей ({symbol} {timeframe}): {e}")
        return None


def test_connection():
    """Проверяет подключение и выводит баланс"""
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
