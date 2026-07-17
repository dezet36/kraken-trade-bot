import os
from dotenv import load_dotenv
from logger import log

# ── DATA_DIR: каталог персистентных данных, ФИЗИЧЕСКИ отдельный от кода ──────
# Проблема, которую это решает: если код деплоится ручным копированием папки
# Live_Bot поверх старой (без git), любой файл ВНУТРИ Live_Bot/ (журнал сделок,
# БД, состояние позиций, .env) стирается локальной версией при каждом обновлении.
# BOT_DATA_DIR (реальная переменная окружения ОС, не строка внутри .env — иначе
# курица-яйцо) указывает на каталог ВНЕ Live_Bot (в Docker — отдельный volume).
# Не задан -> поведение как раньше (данные внутри Live_Bot, для локальной разработки).
DATA_DIR = os.getenv('BOT_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)

# .env читаем СНАЧАЛА из DATA_DIR (переживает перезапись Live_Bot/), иначе —
# обычный поиск python-dotenv (Live_Bot/.env, как раньше).
_env_in_data = os.path.join(DATA_DIR, '.env')
if os.path.exists(_env_in_data):
    env_loaded = load_dotenv(_env_in_data)
    log(f"🔧 .env загружен из DATA_DIR ({_env_in_data}): {env_loaded}")
else:
    env_loaded = load_dotenv()
    log(f"🔧 .env загружен: {env_loaded}")

# Режим торговли и биржа
TRADING_MODE = os.getenv('TRADING_MODE', 'DEMO')
EXCHANGE_NAME = os.getenv('EXCHANGE', 'bybit')

# ── Пул пар для сканирования ─────────────────────────────────────────────────
# Решение 2026-07-05 (v3 = геометрия v2 + тайм-стоп 14д; 12-мес модель; выбор
# пользователя): 16 пар с положительным индивидуальным edge под v3 (sumR>0,
# PF>=1.0, обе половины года > -10R, n>=40) при cap=5.
# Модель года: +252.3% / DD 17.9% / 33.7 сд/нед / WR 34.7% / PF 1.304 — доминирует
# конфигурацию 10 пар/cap4 (+133.1% / DD 20.9%) по прибыли И просадке.
# Оговорка: отбор по знаку на одном годе (in-sample); все 10 прежних пар прошли
# фильтр сами, добавлены 6: ZEC, SUI, ARB, DOT, XLM, SHIB1000 (1000-контракт).
TRADING_PAIRS_POOL = [
    'BTCUSDT',  'ETHUSDT',  'SOLUSDT',  'XRPUSDT',    'BNBUSDT',
    'DOGEUSDT', 'ADAUSDT',  'AVAXUSDT', 'LINKUSDT',   'LTCUSDT',
    'ZECUSDT',  'SUIUSDT',  'ARBUSDT',  'DOTUSDT',    'XLMUSDT',
    'SHIB1000USDT',
]

# Пары для торговли (legacy — используется в логах и confirm_live_mode)
TRADING_PAIRS = TRADING_PAIRS_POOL

# Ограничения
MAX_ACTIVE_PAIRS = int(os.getenv('MAX_ACTIVE_PAIRS', 5))   # макс. одновременных позиций
# (v3 2026-07-05: 5 — на пуле из 16 пар cap=5 даёт лучший фронт прибыль/просадка;
#  одновременный риск 5 × 0.5% = 2.5% депозита)

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
# GTC-лимит живёт до 72ч (v3-sweep 2026-07-05: обрезка ожидания филла монотонно
# ухудшает модель: 4ч +170.2% / 12ч +180.5% / 24ч +186.6% / 72ч +197.6% —
# прежние 4ч недобирали ~27пп годовых; инвалидация/TP-touch отменяют ордер раньше
# независимо от срока, кулдаун 12ч истекает до отмены — ре-детекция как в модели)
PENDING_ORDER_MAX_HOURS = float(os.getenv('PENDING_ORDER_MAX_HOURS', 72.0))

# Таймфреймы
TIMEFRAME_MAJOR = '1h'
TIMEFRAME_MINOR = '5m'
LOOKBACK_CANDLES = 48

# ── HTF фильтр тренда ────────────────────────────────────────────────────────
HTF_TIMEFRAME     = '4h'   # старший таймфрейм для определения тренда
HTF_EMA_FAST      = 50     # быстрая EMA
HTF_EMA_SLOW      = 200    # медленная EMA
HTF_ALLOW_NEUTRAL = True   # True = торговать оба направления при нейтральном тренде

# ── Стратегия «Фибо-лимит» ────────────────────────────────────────────────────
# Вход — GTC-лимит на границе зоны A (38.2% уровень), БЕЗ ожидания BoS.
# Тейк/стоп — см. блок «ГЕОМЕТРИЯ v2» ниже (TP1_LEVEL/SL_LEVEL_R/TP_CLOSE_FRACTIONS).
# Безубыток при пробое уровня B импульса (BREAKEVEN_AT_B).
ENTRY_MODE          = 'ZONE_LIMIT'   # лимит в зоне A (не BoS-вход)
REQUIRE_BOS         = False          # BoS-подтверждение основного входа ВРЕДНО (бэктест)
USE_ZONE_B_ENTRY    = False          # глубокая зона B не окупается — отключена
BREAKEVEN_AT_B      = True           # SL -> вход при пробое уровня B (0%, конец импульса)

# ── Трейлинг-стоп (отключён — текущая модель: TP_CLOSE_FRACTIONS + безубыток@B) ──
TRAIL_AFTER_TP    = 99     # 99 = трейлинг не активируется
TRAIL_DISTANCE_K  = 1.0    # множитель начального SL-расстояния для трейла

# Зоны интереса
ZONE_A_TOP = 0.618
ZONE_A_BOTTOM = 0.382
ZONE_B_TOP = 0.886
ZONE_B_BOTTOM = 0.786

# Стопы и тейки (уровни расширений от HIGH/LOW импульса)
# ── ГЕОМЕТРИЯ v2 (2026-07-05, сетка 22 конфигов на 12 мес, выбор пользователя) ──
# Стоп перенесён с 61.8% на 88.6% (= уровень инвалидации сетапа): v1 стопился об
# обычный шум коррекции (67% сделок гибли, не дойдя до B). Тейк -18% -> -25%
# (доминирует на всех уровнях стопа). Модель года (10 пар, cap=4, risk 0.5%):
# +107.3% / DD 11.2% / WR 35.7% / PF 1.347 — против v1 +100.7% / DD 23.6% / WR 29.5%.
# Чекпоинт отката: git tag strategy-v1-fibo.
MIN_SL_PERCENT = 0.008
SL_BUFFER = 0.010   # 1.0% от размера импульса — буфер за уровнем стопа
SL_LEVEL_R = 0.886  # уровень стопа как доля коррекции от B (0.618 = v1; 0.886 = инвалидация)
TP1_LEVEL = 0.25    # -25%  единственный таргет (закрываем 100%)
TP2_LEVEL = 0.27    # (не используется при одном TP; оставлен для совместимости журнала)
TP_CLOSE_FRACTIONS = [1.0]   # ОДИН тейк, закрывает 100% позиции (SL+TP на ордере входа)
MIN_RR = 1.1        # страховочный минимум RR (геометрия v2 даёт ~1.23 при WR 35.7%;
                    # старый гейт 2.0 был калиброван под геометрию v1 и в v2 зарубил бы всё)

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

# ── Тайм-стоп позиции (v3, 2026-07-05) ───────────────────────────────────────
# КРИТИЧНО для широкого стопа v2: без лимита позиция (особенно после безубытка,
# стоп=вход) может застрять в коридоре на месяцы и заблокировать пару (в бэктесте
# 6 пар стояли по 9 мес). Sweep {None/14д/7д/3д}: 14 дней — оптимум (+131.9% vs
# +90.6% без лимита, частота +37%, всего ~6 срабатываний/год на 10 парах).
# 0/пусто = выключено.
_mph = os.getenv('MAX_POSITION_HOLD_HOURS', '336').strip()
MAX_POSITION_HOLD_HOURS = float(_mph) if _mph else 0.0

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