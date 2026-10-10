"""
Адаптер стратегии «Против толпы» (CROWD) под интерфейс живого бота.

ЧТО ТОРГУЕТСЯ — crowd/params.py, там же замер. Коротко: выплаченный фандинг
впервые поднялся до +5 б.п. за 8 ч и выше — шорт по рынку, стоп 1.5·ATR(4ч),
цель 2R, не дольше 10 суток.

ВХОД ПО РЫНКУ. Замер входил в минуту выплаты тейкером. Заявка — лимит ЗА
рынком (исполняется сразу); позже часа после выплаты вход не делается.

ДАННЫЕ. Ставки — журнал фандинга общего слоя (analysis.market.series);
если очередная выплата по времени уже прошла, а в журнале её нет, —
analysis.market.settled_funding дозапрашивает её у биржи (один запрос на
пару в момент выплаты). Свечи 4ч — для ATR.
"""

import time

from analysis import market
from analysis.market import fetch_ohlcv
from infra.logger import log
from strategies import scan_report as report
from strategies.crowd import core, params

NAME = 'CROWD'
GRACE_MS = 10_000

_last_reason = {}


def _funding_rows(pair, client, now_ms):
    """Три последние выплаты пары; выплата прошла, а в журнале её нет — дозапрос."""
    rows = market.series('funding', pair, limit=3)
    if rows:
        step = rows[-1]['ts'] - rows[-2]['ts'] if len(rows) >= 2 else core.STEP_MS
        if step <= 0 or step > core.STEP_MS:
            step = core.STEP_MS
        due = now_ms >= rows[-1]['ts'] + step + GRACE_MS
    else:
        due = True
    if due and client is not None:
        market.settled_funding(pair, client=client)
        rows = market.series('funding', pair, limit=3)
    return rows


def _atr_now(pair, client=None):
    """ATR(14) по закрытым свечам 4ч и цена последней сделки — или (None, None)."""
    raw = fetch_ohlcv(params.TIMEFRAME, limit=params.ATR_BARS + 1, symbol=pair, client=client)
    if raw is None or len(raw) < params.ATR_PERIOD + 3:
        return None, None
    price = float(raw['close'].iloc[-1])
    df = raw.iloc[:-1]
    a = core.atr(df['high'].astype(float).tolist(), df['low'].astype(float).tolist(),
                 df['close'].astype(float).tolist(), params.ATR_PERIOD)
    return a[-1], price


def analyze_market(pair, client=None, now_ms=None):
    """Сигнал по паре или None (причина — в _last_reason)."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rows = _funding_rows(pair, client, now_ms)
    ev, reason = core.event(rows, now_ms, params.THRESHOLD_BP, params.SIGNAL_MAX_AGE_MIN)
    if ev is None:
        _last_reason[pair] = reason
        return None
    a, price = _atr_now(pair, client=client)
    if a is None or price is None:
        _last_reason[pair] = 'мало свечей 4ч'
        return None
    st, reason = core.setup(price, a, params.STOP_ATR, params.TARGET_R, params.MIN_STOP_PCT)
    if st is None:
        _last_reason[pair] = reason
        log(f"   {pair}: нет сигнала — {reason}")
        return None
    _last_reason[pair] = 'толпа в лонгах'
    log(f"   {pair}: фандинг {ev['bp']:+.2f} б.п. (было {ev['prev_bp']:+.2f}) — толпа в лонгах | "
        f"шорт, стоп {st['stop_pct']:.2f}% | цель {params.TARGET_R:g}R")
    return _to_bot_signal(st, ev, pair)


def _to_bot_signal(st, ev, pair):
    price = st['ref']
    # лимит за рынком — исполняется сразу, тейкером; доля — предел худшей цены
    entry = price * (1 - params.MARKET_CAP_PCT / 100)
    stop, target = st['stop'], st['target']
    dist = abs(entry - stop)
    rr = abs(target - entry) / dist if dist else 0.0
    why = (f"Толпа в лонгах: фандинг {ev['bp']:+.2f} б.п. за 8 ч (было {ev['prev_bp']:+.2f}, "
           f"порог +{params.THRESHOLD_BP:g}); шорт по рынку, стоп +{params.STOP_ATR:g} ATR 4ч "
           f"({st['stop_pct']:.2f}%), цель {params.TARGET_R:g}R, не дольше "
           f"{params.MAX_POSITION_HOLD_HOURS / 24:.0f} сут")
    return {
        'trading_pair': pair,
        'strategy': NAME,
        # Цена, по которой стратегия считала: по ней брокер исполняет лимит,
        # стоящий за рынком (strategy_profile.fills_through_market).
        'market_price': price,
        'setup': {'type': 'SHORT', 'start_price': stop, 'end_price': target,
                  'size': abs(stop - target), 'start_time': None},
        'params': {
            'entry': entry,
            'stop_loss': stop,
            'take_profit_1': target,
            'take_profit_2': target,
            'tp_targets': [target],
            'tp_fractions': [1.0],
            # Безубытка нет: в замере стоп не двигался.
            'be_level': None,
            'breakeven_after_tp': False,
            'max_hold_hours': params.MAX_POSITION_HOLD_HOURS,
            'rr': rr,
            'sl_distance': dist,
            'invalidation': stop,
        },
        'trigger': {'zone': 'CROWD', 'entry_type': 'LIMIT', 'trigger_price': entry},
        'zone': 'CROWD',
        'htf_trend': 'BEARISH',
        # Очередь — по силе перекоса толпы.
        'score': round(ev['bp'], 3),
        'why': why,
        'crowd': {'funding_bp': ev['bp'], 'prev_bp': ev['prev_bp'], 'paid_at': ev['ts'],
                  'atr': st['atr'], 'stop_pct': st['stop_pct'], 'ref': price, 'rr': rr},
    }


def scan_for_setups(pairs, trade_manager, client=None, now_ms=None):
    """Кандидаты, отсортированные по силе перекоса толпы."""
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
            signal = analyze_market(pair, client=client, now_ms=now_ms)
            report.record(NAME, pair, None if signal else _last_reason.get(pair))
            if signal:
                candidates.append({'pair': pair, 'signal': signal, 'score': signal['score'],
                                   'rr': signal['params']['rr'], 'poi_type': 'CROWD', 'df_1h': None})
        except Exception as exc:                   # noqa: BLE001
            log(f"   {pair}: ошибка сканирования CROWD — {exc}")
            report.record(NAME, pair, f'ошибка сканирования: {exc}')
    report.finish(NAME)
    candidates.sort(key=lambda c: -c['score'])
    return candidates


# ── Единый набор функций реестра стратегий (strategies/registry.py) ──

def scan(pairs, gate, client=None):
    return scan_for_setups(pairs, gate, client=client)


def build_signal(candidate):
    pair = candidate['pair']
    signal = candidate.get('signal')
    if not signal:
        log(f"   CROWD {pair}: кандидат без сигнала — пропускаю")
        return None, None
    cr = signal.get('crowd') or {}
    signal['scan'] = {'score': candidate.get('score'), 'rr_est': candidate.get('rr'),
                      'funding_bp': cr.get('funding_bp'), 'stop_pct': cr.get('stop_pct'),
                      'poi_type': 'CROWD'}
    log(f"\n[CROWD] {pair}: шорт против толпы, фандинг {cr.get('funding_bp', 0):+.2f} б.п., "
        f"стоп {cr.get('stop_pct', 0):.2f}%")
    return signal, candidate.get('df_1h')


def profile():
    # Модуль параметров — заново при каждом вызове: тесты перезагружают его.
    from strategies.crowd import params as p
    return {'expiry_hours': p.PENDING_ORDER_MAX_HOURS, 'cooldown_hours': p.COOLDOWN_HOURS,
            'cost_limit_pct': p.MAX_ENTRY_COST_SHARE_PCT, 'max_hold_hours': p.MAX_POSITION_HOLD_HOURS,
            'drops_at_target': p.CANCEL_PENDING_AT_TARGET,
            # Вход по рынку: лимит за рынком исполняется сразу, как в замере.
            'fills_through_market': p.FILL_THROUGH_MARKET, 'min_stop_pct': p.MIN_STOP_PCT,
            'max_same_direction': p.MAX_SAME_DIRECTION}


def geometry(signal, g):
    cr = signal.get('crowd') or {}
    # Сетап — не фигура на графике, а перекос толпы: на графике — цена решения.
    if cr.get('ref'):
        g.lines.append({'price': float(cr['ref']),
                        'label': f"вход: фандинг {cr.get('funding_bp', 0):+.1f} б.п. — толпа в лонгах",
                        'main': True})
