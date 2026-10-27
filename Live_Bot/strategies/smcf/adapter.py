"""
Адаптер SMCF — SMC с целью на ближайшем нетронутом имбалансе 4ч (замер и
правила — smcf/params.py).

Отбор сетапа — общий контекст структуры (analysis/market_structure, тот же, что
читают SMC, ИИ и панель) со СВОИМИ правилами решений (smcf/params, копия
решений SMC на 27.10.2026). Цель — имбалансы 4ч по определению LuxAlgo
(analysis/smc/imbalance.luxalgo_gaps): история 4ч длиннее окна контекста
(цель бывает старше полугода), поэтому её стратегия берёт сама — двумя
запросами раз в 4 часа на пару.
"""

import time

import pandas as pd

from analysis import market_structure as structure
from analysis.market import fetch_ohlcv
from analysis.smc import imbalance
from infra.logger import log
from strategies import scan_report as report
from strategies import settings
from strategies.smcf import params

NAME = 'SMCF'
TF_MS = {'1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000}
PAGE = 1000

_last_reason = {}
_history = {}           # пара -> (номер бара старшего ТФ, свечи)


def _ms(stamps):
    """Миллисекунды UTC из колонки времени (с поясом или без, любой единицы)."""
    idx = pd.DatetimeIndex(stamps)
    if idx.tz is not None:
        idx = idx.tz_convert('UTC').tz_localize(None)
    return idx.as_unit('ms').asi8


def _apply_settings():
    """Минимальный стоп — настройка оператора для SMCF (своя, не SMC)."""
    for name in params.DECISION:
        setattr(params, name, settings.min_stop_pct(NAME))


def _closed_htf(pair, client=None, now_ms=None):
    """Закрытые свечи старшего ТФ за TARGET_HISTORY_BARS; кэш до следующего бара."""
    step = TF_MS[params.TARGET_TF]
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    bar = now // step
    hit = _history.get(pair)
    if hit is not None and hit[0] == bar:
        return hit[1]
    parts, since = [], (bar - params.TARGET_HISTORY_BARS) * step
    while since < bar * step:
        df = fetch_ohlcv(params.TARGET_TF, limit=PAGE, symbol=pair, client=client, since=since)
        if df is None or not len(df):
            break
        parts.append(df)
        since = int(_ms(df['timestamp'])[-1]) + step
        if len(df) < PAGE:
            break
    if not parts:
        return None
    df = pd.concat(parts).drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    df = df[_ms(df['timestamp']) + step <= now].reset_index(drop=True)   # формирующаяся — не в счёт
    _history[pair] = (bar, df)
    return df


def nearest_target(gaps, entry, stop, direction):
    """Ближайший нетронутый имбаланс в сторону сделки не ближе TARGET_MIN_R."""
    side = 1 if direction in ('BULLISH', 'LONG') else -1
    risk = abs(entry - stop)
    edges = [g['edge'] for g in gaps
             if g['target_side'] == side and not g['touched']
             and (g['edge'] - entry) * side >= params.TARGET_MIN_R * risk]
    return min(edges, key=lambda e: abs(e - entry)) if edges else None


def crowd_reason(direction, rate):
    """Причина отказа «толпа за сделку» или None — своя копия правила SMC."""
    if not params.FUNDING_AGAINST_CROWD or rate is None:
        return None
    side = 1.0 if direction in ('BULLISH', 'LONG') else -1.0
    if rate * 1e4 * side <= params.FUNDING_MAX_BP:
        return None
    crowd = 'в лонгах' if rate > 0 else 'в шортах'
    return f'толпа за сделку (фандинг {rate * 1e4:+.2f} б.п., толпа {crowd})'


def _funding_rate(pair, client=None):
    try:
        from analysis import market
        rate = market.settled_funding(pair, client=client)
        return None if rate is None else float(rate)
    except Exception as exc:                                  # noqa: BLE001
        log(f"   {pair}: фандинг не прочитан ({exc})")
        return None


def _signal(setup, pair, target, rate):
    trade = setup['params']
    entry, stop = trade['entry'], trade['stop_loss']
    risk = abs(entry - stop)
    rr = abs(target - entry) / risk if risk else 0.0
    leg, poi = setup['leg'], setup['poi']
    return {
        'trading_pair': pair,
        'setup': {'type': 'LONG' if setup['direction'] == 'BULLISH' else 'SHORT',
                  'start_price': leg['start']['price'], 'end_price': leg['end']['price'],
                  'size': leg['size'], 'start_time': leg['start']['time'], 'end_time': leg['end']['time']},
        'trigger': {'zone': poi['type'], 'entry_type': params.ENTRY_MODE, 'trigger_price': entry},
        'params': {'entry': entry, 'stop_loss': stop, 'take_profit_1': target, 'take_profit_2': target,
                   'be_level': None, 'breakeven_after_tp': False,
                   'tp_targets': [target], 'tp_fractions': [1.0], 'rr': rr, 'sl_distance': trade['sl_distance']},
        'htf_trend': setup['direction'],
        'smcf': {
            'poi_type': poi['type'], 'poi_top': poi['top'], 'poi_bottom': poi['bottom'],
            'confluence': setup['confluence'], 'factors': setup['factors'],
            'target': target, 'target_r': round(rr, 2), 'target_tf': params.TARGET_TF,
            # цели ядра, по которым сетап прошёл R:R 4 (для разбора — исполнение их не видит)
            'fib_targets': list(trade['targets']), 'rr_fib': trade['rr'],
            'funding_bp': None if rate is None else round(rate * 1e4, 3),
        },
    }


def analyze_market(pair, client=None, now_ms=None):
    """Сигнал по паре или None (причина — в _last_reason)."""
    _apply_settings()
    context = structure.get(pair, client=client)
    if context is None:
        _last_reason[pair] = 'мало данных по паре'
        return None
    poi_df = context.frames['poi']
    setup, reason = context.evaluate(len(poi_df) - 1, decision=params)
    if setup is None:
        _last_reason[pair] = reason
        return None
    rate = _funding_rate(pair, client=client)
    blocked = crowd_reason(setup['direction'], rate)
    if blocked:
        _last_reason[pair] = blocked
        return None
    htf = _closed_htf(pair, client=client, now_ms=now_ms)
    if htf is None or len(htf) < 3:
        _last_reason[pair] = f'нет свечей {params.TARGET_TF} для цели'
        return None
    last_close = _ms(htf['timestamp'])[-1] + TF_MS[params.TARGET_TF]
    later = poi_df[_ms(poi_df['timestamp']) >= last_close]
    gaps = imbalance.luxalgo_gaps(htf, later=later)
    trade = setup['params']
    target = nearest_target(gaps, trade['entry'], trade['stop_loss'], setup['direction'])
    if target is None:
        _last_reason[pair] = f'нет нетронутого имбаланса {params.TARGET_TF} не ближе {params.TARGET_MIN_R:g}R'
        return None
    signal = _signal(setup, pair, target, rate)
    log(f"   {pair}: SMCF {setup['direction']} | confluence {setup['confluence']} | "
        f"цель — имбаланс {params.TARGET_TF} {target:.6g} ({signal['params']['rr']:.1f}R)")
    return signal


def scan_for_setups(pairs, trade_manager, client=None):
    candidates = []
    report.begin(NAME)
    for pair in pairs:
        if params.TRADE_POOL and pair not in params.TRADE_POOL:
            report.record(NAME, pair, 'вне пула SMCF')
            continue
        try:
            if not trade_manager.check_cooldown(pair):
                report.record(NAME, pair, 'кулдаун активен')
                continue
            if trade_manager.has_position_or_order(pair):
                report.record(NAME, pair, 'позиция или ордер уже есть')
                continue
            signal = analyze_market(pair, client=client)
            report.record(NAME, pair, None if signal else _last_reason.get(pair))
            if signal:
                context = structure.cached(pair)
                candidates.append({'pair': pair, 'signal': signal, 'score': signal['smcf']['confluence'],
                                   'rr': signal['params']['rr'], 'poi_type': signal['smcf']['poi_type'],
                                   'df_1h': context.frames['poi'] if context else None})
        except Exception as exc:                               # noqa: BLE001
            log(f"   {pair}: ошибка сканирования SMCF — {exc}")
            report.record(NAME, pair, f'ошибка сканирования: {exc}')
    report.finish(NAME)
    # Порядок одновременных сетапов — как у SMC в замере: конфлюенс, затем R:R ядра.
    candidates.sort(key=lambda c: (-c['score'], -c['signal']['smcf']['rr_fib']))
    return candidates


# ── Единый набор функций реестра стратегий (strategies/registry.py) ──

def scan(pairs, gate, client=None):
    return scan_for_setups(pairs, gate, client=client)


def build_signal(candidate):
    pair = candidate['pair']
    signal = candidate['signal']
    info = signal['smcf']
    signal['scan'] = {
        'confluence': info['confluence'], 'poi_type': info['poi_type'],
        'factors': [k for k, ok in info['factors'].items() if ok],
        'target_r': info['target_r'], 'rr_fib': info['rr_fib'], 'funding_bp': info['funding_bp'],
    }
    log(f"\n[SMCF] {pair}: зона {info['poi_type']}, confluence {info['confluence']}, "
        f"цель — имбаланс {info['target_tf']} ({info['target_r']:.1f}R)")
    return signal, candidate.get('df_1h')


def profile():
    # Модуль параметров — заново при каждом вызове: тесты перезагружают его.
    from strategies.smcf import params as p
    return {'expiry_hours': p.PENDING_ORDER_MAX_HOURS, 'cooldown_hours': p.COOLDOWN_HOURS,
            'cost_limit_pct': p.MAX_ENTRY_COST_SHARE_PCT, 'max_hold_hours': p.MAX_POSITION_HOLD_HOURS,
            'drops_at_target': p.CANCEL_PENDING_AT_TARGET, 'fills_through_market': p.FILL_THROUGH_MARKET,
            'max_same_direction': p.MAX_SAME_DIRECTION}


def geometry(signal, g):
    from strategies import glossary
    info = signal.get('smcf') or {}
    g.band(info.get('poi_bottom'), info.get('poi_top'), glossary.poi_type(info.get('poi_type')), main=True)
    g.leg('начало движения', 'конец движения')
    if info.get('target'):
        g.lines.append({'price': float(info['target']),
                        'label': f"цель · имбаланс {info.get('target_tf', '4h')}"})
