"""
ИИ в режиме «правила» (LLM_MODE=rules): сетап — по своим правилам отбора,
проверенным на истории; модель в выборе входа не участвует.

ПОЧЕМУ ТАК (26.09.2026, docs/ИИ_замечания_на_проверку.md, п. 58–61):

  • доктрина, по которой модель строила планы сама (сторона по 4ч и дню,
    стоп за HL часа, ближняя ликвидность ≥2R, безубыток +1R), исполненная
    без единой ошибки кодом, теряет −0.098R на сделку [−0.135; −0.061] на
    4913 сделках пяти периодов 2022–2026 (research/ai_doctrine.py);
  • её правила оставляют худшую четверть сетапов SMC (−0.145R) и запрещают
    лучшие — вынос под HL в нетронутый ордер-блок (+0.230R)
    (research/ai_vs_smc_reasons.py);
  • своё суждение модели о сетапе ОБРАТНО исходу: на 600 анонимных сетапах
    её лучшие 5 из 10 — −0.036R против +0.066R у «всё подряд» и +0.189R у
    статистики, AUC 0.443 (опыт на сервере, scratchpad llm_exp);
  • правила отбора SMC на её 10 крупных монетах — единственный каркас без
    минуса: +141R, +0.171R на сделку в портфеле с живыми правилами брокера,
    пять периодов (research/results/broker_rules_smc_*_close.txt);
  • входы в сутках вокруг решения ФРС у этого каркаса хуже: −0.476R против
    +0.207R [разность −1.19; −0.08], хуже в 4 периодах из 5.

ИЗОЛЯЦИЯ (CLAUDE.md). Структура — общий слой (market_structure, smc/ часть I).
Правила решений — СВОИ: ниже замороженная копия значений smc/params.DECISION
на 26.09.2026. Ядро smc.signal принимает их аргументом (evaluate(decision=)),
поэтому правка параметров SMC ИИ не меняет, а правка этих — не меняет SMC.
Стратегию SMC модуль не импортирует.

Режим выключен по умолчанию (config.LLM_MODE='plans'): включается одной
переменной, прежний режим планов модели остаётся как был.
"""

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import config
from logger import log

NAME = 'LLM'


def enabled():
    return str(getattr(config, 'LLM_MODE', 'plans')).strip().lower() == 'rules'


# ── Правила решений: своя копия (значения smc/params.DECISION на 26.09.2026) ──
# Менять — только с замером на пяти периодах (research/ai_rules_backtest.py) и
# записью в docs/ИИ_замечания_на_проверку.md: это то, с чем считался плюс.
DECISION = SimpleNamespace(
    POI_ENTRY_DEPTH=0.0,
    POI_ENTRY_OFFSET=0.0,
    POI_TYPES_ENABLED=('ORDER_BLOCK',),
    REQUIRE_PREMIUM_DISCOUNT=True,
    REQUIRE_KILLZONE=True,
    KILLZONE_AS_GATE=False,
    CONFLUENCE_WEIGHTS={'htf_bias_aligned': 1.0, 'premium_discount': 1.0, 'poi_fresh': 0.8,
                        'ote_zone': 1.2, 'structure_break': 0.8, 'fvg_present': 0.6,
                        'liquidity_swept': 0.3, 'law_of_effort': 0.1, 'killzone': 0.0},
    MIN_CONFLUENCE_SCORE=4.5,
    LONG_CONFLUENCE_PREMIUM=0.0,
    REGIME_RISK_SCALE=0.5,
    RISK_PER_TRADE_PCT=1.0,
    MAX_TOTAL_RISK_PCT=5.0,
    MIN_RR=4.0,
    MAX_RR=0.0,
    REQUIRE_OTE=False,
    LEG_BARS_MIN=0,
    LEG_BARS_MAX=0,
    SL_BUFFER_PCT=0.0015,
    MIN_SL_PCT=0.005,
    SL_MODE='conservative',
    TP_MODE='fib',
    LIQ_MIN_R=1.0,
    LIQ_MERGE_PCT=0.0015,
    LIQ_MIN_WEIGHT=0.0,
    TP_CLOSE_FRACTIONS=(0.25, 0.25, 0.50),
    ENTRY_MODE='POI_LIMIT',
    PENDING_ORDER_MAX_HOURS=48.0,
    MAX_ENTRY_COST_SHARE_PCT=10.0,
    MAX_POSITION_HOLD_HOURS=336.0,
    COOLDOWN_HOURS=12.0,
    MAX_SAME_DIRECTION=3,
    RANKED_POOL=(),
    MIN_VOLUME_24H_USD=50_000_000.0,
    BREAKEVEN_AFTER_TP1=False,
    TP1_R_MULTIPLE=1.5,
    SKIP_TARGET_TAKEN=False,
    CANCEL_PENDING_AT_TARGET=False,
    FILL_THROUGH_MARKET=False,
)

# Свой пул: плюс каркаса живёт на крупных ликвидных монетах. На десяти
# остальных парах общего списка те же правила дали −45.3R (−0.072R на сделку,
# research/results/ai_smc_pairs.txt).
POOL = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT',
        'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT')

# ── События: решения ФРС (публикация 14:00 по Нью-Йорку ≈ 18:00 UTC) ──────────
# Окно ±24 ч: входов нет. Календарь — federalreserve.gov/monetarypolicy/
# fomccalendars.htm; когда он кончается, модуль пишет об этом в журнал раз
# в сутки — продлить строкой LLM_EVENT_DATES в .env (даты через запятую).
FOMC_2026 = ('2026-01-28', '2026-03-18', '2026-04-29', '2026-06-17',
             '2026-07-29', '2026-09-16', '2026-10-28', '2026-12-09')
EVENT_WINDOW_H = 24.0
H_MS = 3_600_000


def event_dates():
    raw = str(getattr(config, 'LLM_EVENT_DATES', '') or '').strip()
    dates = [d.strip() for d in raw.split(',') if d.strip()] if raw else list(FOMC_2026)
    out = []
    for d in dates:
        try:
            out.append(int(datetime.fromisoformat(d).replace(hour=18, tzinfo=timezone.utc).timestamp() * 1000))
        except ValueError:
            log(f'   {NAME}: дата события «{d}» не разобрана — пропущена')
    return sorted(out)


def event_near(now_ms=None, window_h=EVENT_WINDOW_H):
    """Ближайшее решение ФРС в пределах окна: (дата 'YYYY-MM-DD', часов до/после) или None."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    for t in event_dates():
        hours = (t - now_ms) / H_MS
        if abs(hours) <= window_h:
            return datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime('%Y-%m-%d'), round(hours, 1)
    return None


_calendar_warned = {'day': None}


def _warn_if_calendar_ends(now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    day = now_ms // (24 * H_MS)
    if _calendar_warned['day'] == day:
        return
    future = [t for t in event_dates() if t > now_ms]
    if not future or future[-1] - now_ms < 45 * 24 * H_MS:
        _calendar_warned['day'] = day
        last = (datetime.fromtimestamp(future[-1] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
                if future else 'нет')
        log(f'   {NAME}: календарь решений ФРС кончается ({last}) — продлите LLM_EVENT_DATES')


# ── Сигнал брокеру ────────────────────────────────────────────────────────────
def to_signal(setup, pair):
    """Сетап ядра → сигнал брокеру (формат тот же, что у остальных стратегий)."""
    import settings_store as settings
    trade = setup['params']
    direction = 'LONG' if setup['direction'] == 'BULLISH' else 'SHORT'
    leg = setup['leg']
    poi = setup['poi']
    targets = list(trade['targets'])
    params = {
        'entry': trade['entry'],
        'stop_loss': trade['stop_loss'],
        'take_profit_1': targets[0],
        'take_profit_2': targets[1] if len(targets) > 1 else targets[0],
        # Без безубытка: замер правил шёл без него (DECISION.BREAKEVEN_AFTER_TP1).
        'be_level': None,
        'breakeven_after_tp': bool(DECISION.BREAKEVEN_AFTER_TP1),
        'tp_targets': targets,
        'tp_fractions': list(trade['fractions']),
        'max_same_direction': DECISION.MAX_SAME_DIRECTION,
        'risk_pct': settings.risk_pct(NAME),
        'rr': trade['rr'],
        'sl_distance': trade['sl_distance'],
        'invalidation': poi.get('invalidation'),
    }
    sweep = setup.get('sweep') or {}
    structure = setup.get('structure') or {}
    why = (f"нетронутый ордер-блок {poi['bottom']:.6g}–{poi['top']:.6g} "
           f"{'в дисконте' if direction == 'LONG' else 'в премии'} по старшему тренду, "
           f"конфлюенс {setup['confluence']}"
           + (f", снята ликвидность {sweep.get('source')}" if sweep.get('source') else '')
           + (f", слом {structure.get('type')} у {structure.get('level'):.6g}"
              if structure.get('type') and structure.get('level') else ''))
    return {
        'trading_pair': pair,
        'strategy': NAME,
        'setup': {'type': direction, 'start_price': leg['start']['price'],
                  'end_price': leg['end']['price'], 'size': leg['size'],
                  'start_time': leg['start']['time'], 'end_time': leg['end']['time']},
        'trigger': {'zone': poi['type'], 'entry_type': DECISION.ENTRY_MODE,
                    'trigger_price': trade['entry']},
        'params': params,
        'htf_trend': setup['direction'],
        # Поле 'llm' читают журнал, панель и Telegram у стратегии ИИ.
        'llm': {
            'mode': 'rules',
            'analysis': why,
            'why': why,
            'stop_why': f"за дальним краем блока {poi.get('invalidation'):.6g} + "
                        f"{DECISION.SL_BUFFER_PCT * 100:.2f}% — там идея входа от блока сломана"
            if poi.get('invalidation') else 'за краем блока',
            'tp_why': 'расширения Фибоначчи от ноги, выход 25/25/50',
            'rr': trade['rr'],
            'votes': setup['confluence'],
            'confluence': setup['factors'],
            'trigger_when': 'now',
        },
        'smc': {
            'poi_type': poi['type'], 'poi_top': poi['top'], 'poi_bottom': poi['bottom'],
            'confluence': setup['confluence'], 'factors': setup['factors'],
            'targets': targets, 'fractions': trade['fractions'],
            'rr_first': trade['rr_first'], 'rr_final': trade['rr_final'],
            'sweep': sweep.get('source'), 'sl_mode': trade['sl_mode'],
            'structure_type': structure.get('type'), 'structure_level': structure.get('level'),
        },
    }


# ── Сканер ────────────────────────────────────────────────────────────────────
def scan(pairs, gate, client=None, balance=None, now_ms=None, context_of=None):
    """
    Сетапы по своим правилам на своём пуле. -> список кандидатов для bot.

    context_of(pair) -> MarketContext — откуда брать структуру (тесты
    подменяют; в бою — общий слой market_structure).
    """
    import scan_report as report
    if context_of is None:
        import market_structure

        def context_of(pair):
            return market_structure.get(pair, client=client)

    balance = config.BALANCE if balance is None else balance
    report.begin(NAME)
    _warn_if_calendar_ends(now_ms)
    event = event_near(now_ms)
    candidates = []
    for pair in pairs:
        if pair not in POOL:
            continue
        try:
            if gate.has_position_or_order(pair):
                report.record(NAME, pair, 'позиция или ордер уже есть')
                continue
            if not gate.check_cooldown(pair):
                report.record(NAME, pair, 'кулдаун активен')
                continue
            context = context_of(pair)
            if context is None:
                report.record(NAME, pair, 'мало данных по паре')
                continue
            last = len(context.frames['poi']) - 1
            setup, why = context.evaluate(last, balance=balance, decision=DECISION)
            if setup is None:
                report.record(NAME, pair, why)
                continue
            if event:
                # Сетап найден, но сутки вокруг решения ФРС: отказ пишется с
                # причиной — иначе пропуск выглядит как «ИИ перестал находить».
                date, hours = event
                reason = f'решение ФРС {date} ({hours:+.0f} ч) — входов нет'
                log(f'   {NAME} {pair}: {reason}')
                report.record(NAME, pair, reason)
                continue
            signal = to_signal(setup, pair)
            report.record(NAME, pair, None)
            log(f"   {NAME} {pair}: {signal['setup']['type']} от блока, конфлюенс "
                f"{setup['confluence']}, R:R {setup['params']['rr']:.2f}")
            candidates.append({'pair': pair, 'signal': signal, 'score': setup['confluence'],
                               'rr': setup['params']['rr'], 'poi_type': setup['poi']['type'],
                               'df_1h': context.frames['poi']})
        except Exception as exc:                   # noqa: BLE001
            log(f'   {NAME} {pair}: ошибка сканирования по правилам — {exc}')
            report.record(NAME, pair, f'ошибка сканирования: {exc}')
    report.finish(NAME)
    candidates.sort(key=lambda c: (-c['score'], -c['rr']))
    return candidates
