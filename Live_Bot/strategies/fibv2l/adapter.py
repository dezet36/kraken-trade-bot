"""
Адаптер «Фибоначчи V2 · стоп 0.68» (FIBV2L) под интерфейс живого бота.

ЧТО ТОРГУЕТСЯ — fibv2l/params.py, там же замер. Коротко: быстрый импульс часа,
зафиксированный первой коррекцией к 38.2%; шорт — если BTC за 30 дней рос,
лонг — если пробит максимум 7 суток; лимит на 0.5, стоп за имбалансом глубже входа, иначе за 0.68,
тейки по 25% на 0 (+ безубыток), −0.18, −0.27, −0.618.

Это НОВАЯ стратегия рядом со старой ФИБО (V1) — та работает как работала
(решение владельца 28.10). Рядом же FIBV2 — та же схема со стопом за началом импульса.

КОГДА СТАВИТСЯ ЗАЯВКА. Импульс фиксирует формирующаяся свеча, когда её
экстремум дошёл до 38.2% отката; бот смотрит пары раз в 5 минут и ставит
заявку в этот же час. Один импульс — одна заявка: повтор в тот же час (после
снятия) не ставится.
"""

from datetime import datetime, timezone

import pandas as pd

from strategies import scan_report as report
# Данные — через дверь анализа (analysis/market.py), не с биржи напрямую.
from analysis.market import fetch_ohlcv
from strategies.fibv2l import core, params
from infra.logger import log

NAME = 'FIBV2L'
SECTION = 'fibv2l'

_last_reason = {}
_signaled = {}          # пара -> импульс, по которому уже был сигнал


def _hour(now):
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is not None:
        now = now.tz_convert('UTC').tz_localize(None)
    return now.floor('h')


def _btc_return(client=None):
    """Изменение BTC за 30 дней по закрытым часам (доля) или None."""
    n = params.BTC_LOOKBACK_HOURS
    raw = fetch_ohlcv(params.TIMEFRAME, limit=n + 2, symbol=params.BTC_SYMBOL, client=client)
    if raw is None or len(raw) < n + 2:
        return None
    closes = raw['close'].astype(float).tolist()[:-1]
    return closes[-1] / closes[-1 - n] - 1 if closes[-1 - n] > 0 else None


def analyze_market(pair, client=None, now=None):
    """Сигнал по паре или None (причина — в _last_reason)."""
    raw = fetch_ohlcv(params.TIMEFRAME, limit=params.HISTORY_BARS + 1, symbol=pair, client=client)
    if raw is None or len(raw) < 50:
        _last_reason[pair] = 'мало данных по паре'
        return None
    last = pd.Timestamp(raw['timestamp'].iloc[-1])
    if last.tzinfo is not None:
        last = last.tz_convert('UTC').tz_localize(None)
    if last != _hour(now):
        _last_reason[pair] = 'нет формирующейся свечи текущего часа'
        return None
    df = raw.iloc[:-1].reset_index(drop=True)
    o, h, l, c = (df[k].astype(float).tolist() for k in ('open', 'high', 'low', 'close'))
    bar_high, bar_low = float(raw['high'].iloc[-1]), float(raw['low'].iloc[-1])
    price = float(raw['close'].iloc[-1])
    btc = _btc_return(client) if params.SHORT_NEEDS_BTC_RISE else None
    setup, reason = core.evaluate(o, h, l, c, bar_high, bar_low, price, btc, params)
    _last_reason[pair] = reason
    if setup is None:
        return None
    key = (setup['direction'], str(df['timestamp'].iloc[setup['s']]), str(df['timestamp'].iloc[setup['e']]))
    if _signaled.get(pair) == key:
        _last_reason[pair] = 'по этому ходу сигнал уже был'
        return None
    _signaled[pair] = key
    log(f"   {pair}: импульс {setup['side']} {setup['start']:.6g} → {setup['end']:.6g} "
        f"({setup['size_pct']:.1f}%, {setup['bars']} св., {setup['atr_per_bar']:.2f} ATR/св.) | "
        f"вход {setup['entry']:.6g} | стоп {setup['stop_pct']:.2f}%")
    return _to_bot_signal(setup, pair, df, price)


def _stamp(df, idx):
    try:
        st = pd.Timestamp(df['timestamp'].iloc[idx])
        if st.tzinfo is not None:
            st = st.tz_convert('UTC').tz_localize(None)
        return st.strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:                              # noqa: BLE001
        return None


def _to_bot_signal(setup, pair, df, price):
    is_long = setup['side'] == 'LONG'
    entry, stop, targets = setup['entry'], setup['stop'], setup['targets']
    dist = abs(entry - stop)
    rr = sum(f * abs(t - entry) for f, t in zip(params.TP_FRACTIONS, targets)) / dist if dist else 0.0
    side_word = 'вверх' if is_long else 'вниз'
    stop_word = ('за началом импульса' if params.STOP_MODE == 'start'
                 else f"на {setup['stop_depth']:.3f} импульса")
    why = (f"Быстрый импульс {side_word} {setup['start']:.6g} → {setup['end']:.6g} "
           f"({setup['size_pct']:.1f}% за {setup['bars']} ч, {setup['atr_per_bar']:.2f} ATR/свеча, "
           f"имбалансов {len(setup['gaps'])}), зафиксирован коррекцией к 38.2%: лимит на 0.5 ({entry:.6g}), "
           f"стоп {stop_word} ({setup['stop_pct']:.2f}%), тейки по 25% на 0 (+ безубыток), −0.18, −0.27, −0.618")
    return {
        'trading_pair': pair,
        'strategy': NAME,
        'market_price': price,
        'setup': {
            'type': setup['side'],
            'start_price': setup['start'],
            'end_price': setup['end'],
            'size': setup['size'],
            'start_time': _stamp(df, setup['s']),
            'end_time': _stamp(df, setup['e']),
        },
        'params': {
            'entry': entry,
            'stop_loss': stop,
            'take_profit_1': targets[0],
            'take_profit_2': targets[-1],
            'tp_targets': list(targets),
            'tp_fractions': list(params.TP_FRACTIONS),
            # Безубыток — после первой цели (конца импульса), как в замере.
            'be_level': None,
            'breakeven_after_tp': True,
            'max_hold_hours': params.MAX_POSITION_HOLD_HOURS,
            'rr': rr,
            'sl_distance': dist,
            'invalidation': stop,
            # Снять заявку, если цена вернулась к концу импульса до налива.
            'cancel_beyond': setup['end'],
        },
        'trigger': {'zone': 'FIB 0.5', 'entry_type': 'LIMIT', 'trigger_price': entry},
        'zone': 'FIB 0.5',
        'htf_trend': 'BULLISH' if is_long else 'BEARISH',
        'score': round(setup['atr_per_bar'], 3),
        'why': why,
        SECTION: {
            'A': setup['start'], 'B': setup['end'], 'entry': entry, 'stop': stop, 'targets': list(targets),
            'stop_mode': params.STOP_MODE, 'stop_depth': setup['stop_depth'], 'stop_pct': setup['stop_pct'],
            'size_pct': setup['size_pct'], 'bars': setup['bars'], 'atr_per_bar': setup['atr_per_bar'],
            'dir_share': setup['dir_share'], 'gaps': len(setup['gaps']), 'kind': setup['kind'], 'rr': rr,
        },
    }


def scan_for_setups(pairs, trade_manager, client=None, now=None):
    """Кандидаты, отсортированные по силе импульса (ATR за свечу)."""
    candidates = []
    report.begin(NAME)
    for pair in pairs:
        try:
            if not trade_manager.check_cooldown(pair):
                report.record(NAME, pair, 'кулдаун активен')
                continue
            if trade_manager.has_position_or_order(pair):
                report.record(NAME, pair, 'позиция или ордер уже есть')
                continue
            signal = analyze_market(pair, client=client, now=now)
            report.record(NAME, pair, None if signal else _last_reason.get(pair))
            if signal:
                candidates.append({'pair': pair, 'signal': signal, 'score': signal['score'],
                                   'rr': signal['params']['rr'], 'poi_type': 'FIB', 'df_1h': None})
        except Exception as exc:                   # noqa: BLE001
            log(f"   {pair}: ошибка сканирования {NAME} — {exc}")
            report.record(NAME, pair, f'ошибка сканирования: {exc}')
    report.finish(NAME)
    candidates.sort(key=lambda x: -x['score'])
    return candidates


# ── Единый набор функций реестра стратегий (strategies/registry.py) ──────────

def scan(pairs, gate, client=None):
    return scan_for_setups(pairs, gate, client=client)


def build_signal(candidate):
    signal = candidate.get('signal')
    if not signal:
        log(f"   {NAME} {candidate.get('pair')}: кандидат без сигнала — пропускаю")
        return None, None
    fb = signal.get(SECTION) or {}
    signal['scan'] = {'score': candidate.get('score'), 'rr_est': candidate.get('rr'),
                      'stop_pct': fb.get('stop_pct'), 'atr_per_bar': fb.get('atr_per_bar'), 'poi_type': 'FIB'}
    log(f"\n[{NAME}] {candidate['pair']}: импульс {signal['setup'].get('type')}, "
        f"стоп {fb.get('stop_pct', 0):.2f}%, {fb.get('atr_per_bar', 0):.2f} ATR/свеча")
    return signal, candidate.get('df_1h')


def profile():
    # Модуль параметров — заново при каждом вызове: тесты перезагружают его.
    from strategies.fibv2l import params as p
    return {'expiry_hours': p.PENDING_ORDER_MAX_HOURS, 'cooldown_hours': p.COOLDOWN_HOURS,
            'cost_limit_pct': p.MAX_ENTRY_COST_SHARE_PCT, 'max_hold_hours': p.MAX_POSITION_HOLD_HOURS,
            'drops_at_target': p.CANCEL_PENDING_AT_TARGET, 'fills_through_market': p.FILL_THROUGH_MARKET,
            'min_stop_pct': p.MIN_STOP_PCT, 'limit_offset_pct': p.LIMIT_OFFSET_PCT,
            'max_same_direction': p.MAX_SAME_DIRECTION}


def geometry(signal, g):
    fb = signal.get(SECTION) or {}
    # Сетап — быстрый импульс и откат к 0.5: нога, вход, стоп и четыре тейка.
    g.leg('начало импульса', 'конец импульса (тейк 1)')
    if fb.get('entry'):
        g.lines.append({'price': float(fb['entry']), 'label': 'откат 0.5 — вход', 'main': True})
    if fb.get('stop'):
        g.lines.append({'price': float(fb['stop']), 'label': 'стоп'})
    for label, price in zip(('−0.18 — тейк 2', '−0.27 — тейк 3', '−0.618 — тейк 4'), (fb.get('targets') or [])[1:]):
        g.lines.append({'price': float(price), 'label': label})
