import os
from dotenv import load_dotenv
from logger import log

# Загружаем .env
env_loaded = load_dotenv()
log(f"🔧 .env загружен: {env_loaded}")

# Режим торговли и биржа
TRADING_MODE = os.getenv('TRADING_MODE', 'DEMO')
EXCHANGE_NAME = os.getenv('EXCHANGE', 'bybit')

# ── Пул пар для сканирования ─────────────────────────────────────────────────
TRADING_PAIRS_POOL = [
    'BTCUSDT',  'ETHUSDT',  'SOLUSDT',  'XRPUSDT',  'BNBUSDT',
    'DOGEUSDT', 'ADAUSDT',  'AVAXUSDT', 'DOTUSDT',  'LINKUSDT',
    'LTCUSDT',  'BCHUSDT',  'TRXUSDT',  'XLMUSDT',  'UNIUSDT',
    'APTUSDT',  'SUIUSDT',  'ARBUSDT',  'OPUSDT',   'TIAUSDT',
    'TAOUSDT',  'HYPEUSDT', 'JUPUSDT',  'WIFUSDT',  'LDOUSDT',
    'PEPEUSDT', 'SHIBUSDT', 'BONKUSDT', 'FLOKIUSDT','XMRUSDT',
    'ZECUSDT',  'LITUSDT',  'PUMPUSDT', 'ASTERUSDT',
]

# Пары для торговли (legacy — используется в логах и confirm_live_mode)
TRADING_PAIRS = TRADING_PAIRS_POOL

# Ограничения
MAX_ACTIVE_PAIRS = int(os.getenv('MAX_ACTIVE_PAIRS', 3))   # макс. одновременных позиций (W11: 3 по бэктесту)

# API ключи
BINGX_API_KEY = os.getenv('BINGX_API_KEY')
BINGX_SECRET_KEY = os.getenv('BINGX_SECRET_KEY')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_SECRET_KEY = os.getenv('BYBIT_SECRET_KEY')

# Диагностика
log(f"🔍 Загружено из .env:")
log(f"   TRADING_MODE: {TRADING_MODE}")
log(f"   EXCHANGE_NAME: {EXCHANGE_NAME}")
log(f"   BYBIT_API_KEY: {'Загружен' if BYBIT_API_KEY else 'НЕ ЗАГРУЖЕН!'}")
log(f"   BYBIT_SECRET_KEY: {'Загружен' if BYBIT_SECRET_KEY else 'НЕ ЗАГРУЖЕН!'}")
log(f"   BINGX_API_KEY: {'Загружен' if BINGX_API_KEY else 'НЕ ЗАГРУЖЕН!'}")

# Риск-менеджмент
RISK_PER_TRADE = float(os.getenv('RISK_PER_TRADE', 0.5))   # % баланса на сделку (W11: 0.5% по бэктесту — DD ~33%)
BALANCE = float(os.getenv('BALANCE', 10000))
RISK_PER_PAIR = RISK_PER_TRADE  # риск фиксирован на сделку; макс. экспозиция = MAX_ACTIVE_PAIRS × RISK_PER_TRADE

# Плечо — устанавливается явно перед каждой сделкой
LEVERAGE = int(os.getenv('LEVERAGE', 20))

# Максимум пар для сканирования в динамическом режиме
MAX_SCAN_PAIRS = int(os.getenv('MAX_SCAN_PAIRS', 60))

# ── Фильтры сканера ──────────────────────────────────────────────────────────
MIN_VOLUME_24H_USD = float(os.getenv('MIN_VOLUME_24H_USD', 50_000_000))  # $50M объём за 24ч
MIN_IMPULSE_PCT    = float(os.getenv('MIN_IMPULSE_PCT', 3.0))            # мин. размер импульса, % от цены

# ── W12: Фильтры качества импульса (импульс, а не долгая торговля) ───────────
MAX_IMPULSE_CANDLES   = int(os.getenv('MAX_IMPULSE_CANDLES', 24))        # импульс не длиннее суток (1H)
MIN_IMPULSE_VELOCITY  = float(os.getenv('MIN_IMPULSE_VELOCITY', 0.30))   # мин. скорость, % размера на свечу

# ── W3: Фильтр микроценовых пар ──────────────────────────────────────────────
MICRO_PRICE_BLACKLIST   = {'SHIBUSDT', 'BONKUSDT', 'FLOKIUSDT', 'PEPEUSDT'}
MIN_ENTRY_PRICE         = float(os.getenv('MIN_ENTRY_PRICE', 0.001))     # пары ниже $0.001 исключаются
MAX_POSITION_SIZE_UNITS = float(os.getenv('MAX_POSITION_SIZE_UNITS', 1_000_000))  # страховочный cap

# ── W9: Лимитный ордер на вход (GTC, без блокирования) ──────────────────────
USE_LIMIT_ENTRY         = os.getenv('USE_LIMIT_ENTRY', 'true').lower() == 'true'
LIMIT_ENTRY_OFFSET_PCT  = float(os.getenv('LIMIT_ENTRY_OFFSET_PCT', 0.001))  # 0.1% cap проскальзывания
PENDING_ORDER_MAX_HOURS = float(os.getenv('PENDING_ORDER_MAX_HOURS', 4.0))   # GTC живёт до 4ч

# Таймфреймы
TIMEFRAME_MAJOR = '1h'
TIMEFRAME_MINOR = '5m'
LOOKBACK_CANDLES = 48

# ── HTF фильтр тренда ────────────────────────────────────────────────────────
HTF_TIMEFRAME     = '4h'   # старший таймфрейм для определения тренда
HTF_EMA_FAST      = 50     # быстрая EMA
HTF_EMA_SLOW      = 200    # медленная EMA
HTF_ALLOW_NEUTRAL = True   # True = торговать оба направления при нейтральном тренде

# ── W11: Стратегия «Фибо-лимит» (по бэктесту конфиг D3) ──────────────────────
# Вход — GTC-лимит на границе зоны A (38.2% уровень), БЕЗ ожидания BoS.
# 2 тейка: TP1 -18% (50%), TP2 -27% (50%). Безубыток при пробое уровня B импульса.
ENTRY_MODE          = 'ZONE_LIMIT'   # лимит в зоне A (не BoS-вход)
REQUIRE_BOS         = False          # BoS-подтверждение основного входа ВРЕДНО (бэктест)
USE_ZONE_B_ENTRY    = False          # глубокая зона B не окупается — отключена
BREAKEVEN_AT_B      = True           # SL -> вход при пробое уровня B (0%, конец импульса)

# ── Трейлинг-стоп (отключён в W11: модель = 2 TP + безубыток@B) ──────────────
TRAIL_AFTER_TP    = 99     # 99 = трейлинг не активируется (только 2 TP)
TRAIL_DISTANCE_K  = 1.0    # множитель начального SL-расстояния для трейла

# Зоны интереса
ZONE_A_TOP = 0.618
ZONE_A_BOTTOM = 0.382
ZONE_B_TOP = 0.886
ZONE_B_BOTTOM = 0.786

# Стопы и тейки (уровни расширений от HIGH/LOW импульса)
MIN_SL_PERCENT = 0.008
SL_BUFFER = 0.010   # 1.0% от размера импульса — реальный буфер за зоной
TP1_LEVEL = 0.18    # -18%  единственный таргет (закрываем 100%)
TP2_LEVEL = 0.27    # -27%  (не используется при одном TP; оставлен для совместимости)
TP_CLOSE_FRACTIONS = [1.0]   # ОДИН тейк -18%, закрывает 100% позиции (SL+TP на ордере входа)
MIN_RR = 2.0        # минимальный RR ко входу (к TP1)

# Volume confirmation (legacy, не используется при REQUIRE_BOS=False)
VOLUME_CONFIRM_MULT = 1.2

# Кулдаун
COOLDOWN_HOURS = 12

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ── Платформа (мульти-юзер SaaS) ─────────────────────────────────────────────
# ID администраторов (через запятую) — доступ к админ-командам платформы.
ADMIN_IDS = [
    int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x.strip().lstrip('-').isdigit()
]