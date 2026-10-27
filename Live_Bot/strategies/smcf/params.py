"""
Параметры решений SMCF — SMC с целью на ближайшем нетронутом имбалансе 4ч.

ОТКУДА (решение владельца 27.10.2026, «делай»). Замер — research/luxsmc/
PROTOCOL_GRAFT.md, вариант X5: стенд живой SMC «глазами бота», 10 пар, фильтр
толпы с порогом 0, фандинг по факту; отбор bear+mid1, приёмка mid2+12m+fresh.
    живая SMC   отбор 178 сд +0.265R/сд | приёмка 131 сд +0.078R | вне пула +0.128R
    SMCF        отбор  95 сд +0.609R/сд | приёмка  74 сд +0.475R | вне пула +0.338R
Принят по правилам протокола, но держится на редких дальних выигрышах: цель
берётся в 16–19% сделок, в среднем около 14R; без трёх лучших сделок приёмка
−0.265R/сд (у SMC −0.079). Поэтому SMCF — КАНДИДАТ на бумаге рядом с SMC, а не
замена ей: судить по живым сделкам (~50).

ЧТО ТОРГУЕТСЯ. Те же сетапы, что у SMC (ордер-блок часа, направление 1д и 4ч,
конфлюенс, R:R 4 по целям Фибоначчи, минимальный стоп, фильтр толпы), но вся
позиция закрывается у ближайшего нетронутого имбаланса 4ч не ближе
TARGET_MIN_R от входа (определение имбаланса — индикатор LuxAlgo,
analysis/smc/imbalance.luxalgo_gaps). Нет такого имбаланса — сетапа нет.

ИЗОЛЯЦИЯ (CLAUDE.md). Ниже — замороженная копия значений
strategies/smc/params.DECISION на 27.10.2026, с которой считался замер: правка
SMC эту стратегию не меняет, правка этих чисел не меняет SMC. Стратегию SMC
пакет не импортирует. Менять — только с замером (research/luxsmc/graft.py).
"""

from infra import params_env

_f, _i, _b, _s = params_env.reader('SMCF')

# ── Отбор сетапа: копия решений SMC (значения на 27.10.2026) ────────────────
POI_ENTRY_DEPTH = 0.0
POI_ENTRY_OFFSET = 0.0
POI_TYPES_ENABLED = ('ORDER_BLOCK',)
REQUIRE_PREMIUM_DISCOUNT = True
REQUIRE_KILLZONE = True
KILLZONE_AS_GATE = False
CONFLUENCE_WEIGHTS = {
    'htf_bias_aligned': 1.0, 'premium_discount': 1.0, 'poi_fresh': 0.8, 'ote_zone': 1.2,
    'structure_break': 0.8, 'fvg_present': 0.6, 'liquidity_swept': 0.3, 'law_of_effort': 0.1,
    'killzone': 0.0,
}
MIN_CONFLUENCE_SCORE = 4.5
LONG_CONFLUENCE_PREMIUM = 0.0
# R:R 4 считается по целям Фибоначчи ядра — так отбирались сетапы в замере;
# цель исполнения — имбаланс (ниже).
MIN_RR = 4.0
MAX_RR = 0.0
REQUIRE_OTE = False
LEG_BARS_MIN = 0
LEG_BARS_MAX = 0
SL_BUFFER_PCT = 0.0015
# Минимальный стоп — как у SMC на сервере (настройка оператора 0.8%); оператор
# меняет его для SMCF отдельно (adapter._apply_settings).
MIN_SL_PCT = 0.008
SL_MODE = 'conservative'
TP_MODE = 'fib'
TP1_R_MULTIPLE = 1.5
LIQ_MIN_R = 1.0
LIQ_MERGE_PCT = 0.0015
LIQ_MIN_WEIGHT = 0.0
TP_CLOSE_FRACTIONS = (0.25, 0.25, 0.50)
SKIP_TARGET_TAKEN = False
ENTRY_MODE = 'POI_LIMIT'

# ── Цель: ближайший нетронутый имбаланс старшего ТФ ─────────────────────────
TARGET_TF = _s('TARGET_TF', '4h')
# Не ближе стольких R от входа: ближе — не окупает издержки (как в замере).
TARGET_MIN_R = _f('TARGET_MIN_R', 1.0)
# Сколько свечей 4ч смотреть назад. В замере цель была старше 30 суток у 65%
# сделок, старше 209 — у 5% (research/luxsmc, 27.10.2026): 2000 свечей ≈ 333 суток.
TARGET_HISTORY_BARS = _i('TARGET_HISTORY_BARS', 2000)
BREAKEVEN_AFTER_TP1 = False

# ── Исполнение и жизнь сетапа — как у SMC в замере ──────────────────────────
PENDING_ORDER_MAX_HOURS = 48.0
COOLDOWN_HOURS = 12.0
MAX_ENTRY_COST_SHARE_PCT = 10.0
MAX_POSITION_HOLD_HOURS = 336.0
CANCEL_PENDING_AT_TARGET = False
FILL_THROUGH_MARKET = False
MAX_SAME_DIRECTION = 3

# ── Пары и толпа — как у SMC ────────────────────────────────────────────────
TRADE_POOL = tuple(
    p.strip() for p in _s('TRADE_POOL', ','.join([
        'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT',
        'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT',
    ])).split(',') if p.strip()
)
# Сетап берётся, только если толпа не стоит в сторону сделки: у лонга ставка
# фандинга ≤ FUNDING_MAX_BP, у шорта ≥ −FUNDING_MAX_BP (как SMC с 14.10.2026).
FUNDING_AGAINST_CROWD = _b('FUNDING_AGAINST_CROWD', True)
FUNDING_MAX_BP = _f('FUNDING_MAX_BP', 0.0)

# Имена, которые вправе менять оператор (adapter._apply_settings).
DECISION = frozenset(('MIN_SL_PCT',))
