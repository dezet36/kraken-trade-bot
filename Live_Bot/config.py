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
# Решение 2026-07-04 (12-мес walk-forward, вариант утверждён пользователем):
# 10 валидированных пар при cap=3 — лучший баланс (модель года: +100.7%, DD 23.6%,
# 26.6 сд/нед, WR 29.5%; подтверждено на out-of-sample половине). Широкие пулы при
# cap=3 проигрывают кратно: слоты — дефицит, среднее качество занятого слота падает
# (27 пар: −27%/DD 46%). Прежний 34-парный пул содержал 5 НЕсуществующих на Bybit
# linear тикеров (PEPE/SHIB/BONK/FLOKI/PUMP — реальные контракты 1000PEPE и т.п.).
# Агрессивная опция в запасе (НЕ деплоена): 18 positive-edge пар + cap=5 —
# модель +139%/DD 31%/55 сд/нед, но отбор по знаку на одном годе (см. CHANGELOG).
TRADING_PAIRS_POOL = [
    'BTCUSDT',  'ETHUSDT',  'SOLUSDT',  'XRPUSDT',  'BNBUSDT',
    'DOGEUSDT', 'ADAUSDT',  'AVAXUSDT', 'LINKUSDT', 'LTCUSDT',
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

# ── Многофакторный скоринг кандидатов (ГИПОТЕЗА, требует проверки после деплоя) ──
# Меняет только порядок попыток среди уже прошедших все гейты кандидатов, когда
# сетапов больше, чем свободных слотов (MAX_ACTIVE_PAIRS). Веса откалиброваны по
# реальному распределению RR/HTF-силы из бэктест-кампании (RR почти константен
# ~2.28 для большинства сетапов — геометрия фиксирована, отсюда умеренный вес RR).
SCORE_WEIGHT_IMPULSE   = 0.35   # размер импульса (единственный эмпирически подтверждённый фактор — W12)
SCORE_WEIGHT_RR        = 0.20   # RR ко входу
SCORE_WEIGHT_HTF       = 0.20   # сила HTF-тренда (тай-брейк — направление уже жёсткий гейт)
SCORE_WEIGHT_PROXIMITY = 0.25   # близость текущей цены к границе входа зоны A
SCORE_RR_CAP           = 3.5    # нормировка RR (чуть выше p75 реального распределения)
SCORE_HTF_STRENGTH_CAP = 0.10   # нормировка |EMA50-EMA200|/EMA200 (4H)
SCORE_NOMINAL_BALANCE  = 10_000.0   # для оценки RR на этапе скана (RR от баланса не зависит)

# Volume confirmation (legacy, не используется при REQUIRE_BOS=False)
VOLUME_CONFIRM_MULT = 1.2

# Кулдаун
COOLDOWN_HOURS = 12

# ── Направленный кэп: макс. позиций/ордеров в ОДНУ сторону (0/пусто = выкл) ──
# Защита от однонаправленной корреляции (LONG на BTC+ETH+SOL = фактический риск
# ~3x номинального). Значение включается после бэктест-вердикта walk_forward.py.
_msd = os.getenv('MAX_SAME_DIRECTION', '0').strip()
MAX_SAME_DIRECTION = int(_msd) if _msd else 0

# ── Сессионный фильтр входов (бэктест 2026-07-04, 6 мес / 10 пар) ────────────
# Сетапы, РОЖДЁННЫЕ в 12-16 UTC (американская сессия), убыточны в 5 из 6 месяцев
# и в обеих половинах периода. Блок рождения новых сетапов в эти часы:
# WR 29.0%->30.7%, PF 1.094->1.229, +23.5%->+53.7% за 6 мес, частота лишь -6.7%.
# Управление ОТКРЫТЫМИ позициями и pending-ордерами НЕ блокируется.
# Пустая строка в env (BLOCK_ENTRY_HOURS_UTC='') полностью выключает фильтр.
BLOCK_ENTRY_HOURS_UTC = frozenset(
    int(h) for h in os.getenv('BLOCK_ENTRY_HOURS_UTC', '12,13,14,15,16').split(',')
    if h.strip() != ''
)

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ── Платформа (мульти-юзер SaaS) ─────────────────────────────────────────────
# ID администраторов (через запятую) — доступ к админ-командам платформы.
ADMIN_IDS = [
    int(x) for x in os.getenv('ADMIN_IDS', '').replace(' ', '').split(',') if x.strip().lstrip('-').isdigit()
]