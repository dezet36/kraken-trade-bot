"""
Фибоначчи V2 · стоп 0.68 (FIBV2L): параметры. Свои; правила — копия FIBV2 на
28.10.2026, кроме стопа (выбор владельца 28.10: «обе — две стратегии на бумаге»).

ЧТО ТОРГУЕТСЯ — как у FIBV2 (быстрый импульс часа, зафиксированный первой
коррекцией к 38.2%; шорт — если BTC за 30 дней рос, лонг — если пробит
максимум 7 суток; лимит на 0.5; тейки по 25% на 0 (+ безубыток), −0.18,
−0.27, −0.618). СТОП — за дальним краем ближайшего имбаланса глубже входа
(окно 0.53–0.886, +0.1% цены); нет такого — за 0.68. Стоп теснее 0.8% цены —
сетап не берётся.

ЧТО ИЗВЕСТНО ДО ЗАПУСКА (та же лаборатория, R на сделку, до 07.2024 / после):
- вместе −0.04 / +0.21 [−0.06; +0.47], выигрышных 38% (у FIBV2 +0.08 / +0.19,
  59%): шорты +0.00 / +0.21, лонги −0.16 / +0.20; стоп за имбалансом — у 80%
  сетапов. Тесный стоп внутри обычной коррекции выбивается шумом чаще.
Кандидат рядом с FIBV2: бумага покажет, какой стоп лучше вживую.
"""
from infra import params_env

_f, _i, _b, _s = params_env.reader('FIBV2L')

TIMEFRAME = '1h'           # не настраивается: схема — часовая
HISTORY_BARS = _i('HISTORY_BARS', 500)     # окно закрытых часов: состояние импульса сходится с полным прогоном

# ── Импульс (определение — analysis/impulse) ────────────────────────────────
FIX_LEVEL = _f('FIX_LEVEL', 0.382)         # первая коррекция к этому уровню фиксирует импульс
START_LOOKBACK = _i('START_LOOKBACK', 24)   # начало «с нуля» — экстремум за столько часов
MAX_TRACK_BARS = _i('MAX_TRACK_BARS', 72)   # дольше без коррекции — слежение заново
ATR_PERIOD = _i('ATR_PERIOD', 14)
MIN_IMPULSE_PCT = _f('MIN_IMPULSE_PCT', 3.0)
MIN_BARS = _i('MIN_BARS', 3)
MAX_BARS = _i('MAX_BARS', 8)
MIN_ATR_PER_BAR = _f('MIN_ATR_PER_BAR', 1.0)   # ход / ATR до импульса / число свечей
MIN_DIR_SHARE = _f('MIN_DIR_SHARE', 0.6)       # доля свечей, закрытых по ходу
MIN_GAPS = _i('MIN_GAPS', 1)                   # имбалансов внутри импульса

# ── Стороны ─────────────────────────────────────────────────────────────────
SHORT_NEEDS_BTC_RISE = _b('SHORT_NEEDS_BTC_RISE', True)
BTC_SYMBOL = _s('BTC_SYMBOL', 'BTCUSDT')
BTC_LOOKBACK_HOURS = _i('BTC_LOOKBACK_HOURS', 720)     # 30 суток
LONG_NEEDS_BREAKOUT = _b('LONG_NEEDS_BREAKOUT', True)
BREAKOUT_LOOKBACK_HOURS = _i('BREAKOUT_LOOKBACK_HOURS', 168)   # максимум 7 суток до начала импульса

# ── Сетка (доли импульса от конца: 0 — конец, 1 — начало, минус — за концом) ─
ENTRY_LEVEL = _f('ENTRY_LEVEL', 0.5)
STOP_MODE = 'level'                            # за имбалансом, иначе за 0.68 (у FIBV2 — 'start')
STOP_BEYOND_START = _f('STOP_BEYOND_START', 1.01)
STOP_LEVEL = _f('STOP_LEVEL', 0.68)            # режим 'level': стоп, если имбаланса глубже входа нет
GAP_STOP_FROM = _f('GAP_STOP_FROM', 0.53)      # режим 'level': имбаланс для стопа не ближе…
GAP_STOP_TO = _f('GAP_STOP_TO', 0.886)         # …и не глубже этого
GAP_STOP_BUFFER_PCT = _f('GAP_STOP_BUFFER_PCT', 0.1)   # стоп за краем имбаланса, % цены
TP_LEVELS = (0.0, -0.18, -0.27, -0.618)
TP_FRACTIONS = (0.25, 0.25, 0.25, 0.25)
MIN_STOP_PCT = _f('MIN_STOP_PCT', 0.8)         # стоп теснее — сетапа нет (не сдвиг!)

# ── Исполнение (strategy_profile) ───────────────────────────────────────────
PENDING_ORDER_MAX_HOURS = _f('PENDING_ORDER_MAX_HOURS', 72.0)
MAX_POSITION_HOLD_HOURS = _f('MAX_POSITION_HOLD_HOURS', 288.0)   # 12 суток — горизонт замера
COOLDOWN_HOURS = _f('COOLDOWN_HOURS', 0.0)       # в замере паузы нет: одна позиция на пару
# Предела издержек нет — решение владельца 28.10 («без порога, стоп ≥ 0.8%»).
# Брокер проверяет долю комиссий в риске; 100% её не ограничивает.
MAX_ENTRY_COST_SHARE_PCT = _f('MAX_ENTRY_COST_SHARE_PCT', 100.0)
LIMIT_OFFSET_PCT = _f('LIMIT_OFFSET_PCT', 0.001)  # лимит на 0.1% хуже уровня — как в замере
CANCEL_PENDING_AT_TARGET = True                # первая цель — конец импульса: дошла без налива — снять
FILL_THROUGH_MARKET = False                    # замер наливал по цене лимита
MAX_SAME_DIRECTION = _i('MAX_SAME_DIRECTION', 0)  # без предела — так считал замер
