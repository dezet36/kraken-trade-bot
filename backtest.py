"""
Backtest: OLD strategy vs NEW strategy (A+B+D improvements)
- 10 liquid pairs, 6 months of 1H + 5M data
- Data cached to disk after first download
- Portfolio management: max 5 concurrent positions, 12h cooldown
"""

import sys, os, pickle, time, math
sys.path.insert(0, r'D:\Bot trade\Live_Bot')

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ── CONFIG ───────────────────────────────────────────────────────────────────
BACKTEST_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT',
    'AVAXUSDT', 'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'LTCUSDT',
]
CACHE_DIR      = r'D:\Bot trade\backtest_cache'
MONTHS_BACK    = 6
INITIAL_BALANCE = 10_000.0
MAX_POSITIONS  = 5
COOLDOWN_HOURS = 12
RISK_PCT       = 1.0   # % per trade
MIN_SL_PCT     = 0.008
SL_BUFFER      = 0.003
MIN_RR         = 1.5
MIN_IMPULSE_PCT = 3.0
MAX_IMPULSE_CANDLES  = 24     # W12: импульс не длиннее суток
MIN_IMPULSE_VELOCITY = 0.30   # W12: мин. скорость, % размера на свечу
LOOKBACK_1H    = 48
TP_FRACTIONS   = [0.25, 0.25, 0.25, 0.25]
TP_LEVELS      = [0.18, 0.27, 0.618, 1.0]
ZONE_A_TOP     = 0.618
ZONE_A_BOTTOM  = 0.382
ZONE_B_TOP     = 0.886
ZONE_B_BOTTOM  = 0.786

# ── Ручки детекции импульса v2.1 (дефолты = текущее live-поведение) ──────────
IMP_ANCHOR_SNAP     = 0      # >0: доснэпить якоря A/B к истинным экстремумам (окно, свечей)
IMP_SOFT_FRACTALS   = False  # True: нестрогое сравнение справа (равные вершины видны)
IMP_FRESH_FALLBACK  = False  # True: свежесть B (последние 24) и для fallback-ветки
IMP_MAX_LEG_RETRACE = None   # доля размера: макс. внутренний откат ноги A->B (None = выкл)

os.makedirs(CACHE_DIR, exist_ok=True)


# ── EXCHANGE (public, no auth needed) ────────────────────────────────────────
def get_exchange():
    return ccxt.bybit({'options': {'defaultType': 'linear'}, 'enableRateLimit': True})


# ── DATA FETCH & CACHE ───────────────────────────────────────────────────────
_TF_MS = {'5m': 5*60*1000, '15m': 15*60*1000, '1h': 60*60*1000, '4h': 4*60*60*1000}

def fetch_ohlcv_full(exchange, symbol, timeframe, since_ms, label='', max_retries=8):
    cache_file = os.path.join(CACHE_DIR, f'{symbol}_{timeframe}.pkl')
    if os.path.exists(cache_file):
        with open(cache_file, 'rb') as f:
            data = pickle.load(f)
        print(f'  [cache] {symbol} {timeframe}: {len(data)} свечей')
        return data

    tf_ms   = _TF_MS.get(timeframe, 60*60*1000)
    now_ms  = int(time.time() * 1000) - tf_ms  # exclude current incomplete candle

    print(f'  [download] {label} {symbol} {timeframe}...', end='', flush=True)
    seen     = set()
    all_candles = []
    cur = since_ms
    fails = 0

    while cur < now_ms:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe, since=cur, limit=1000)
            fails = 0
        except ccxt.BadSymbol as e:
            # символа нет на бирже — retry бессмысленен, сразу наружу
            raise RuntimeError(f'{symbol}: символа нет на бирже ({e})')
        except Exception as e:
            fails += 1
            if max_retries is not None and fails > max_retries:
                raise RuntimeError(f'{symbol} {timeframe}: {fails} ошибок подряд ({e})')
            print(f' RETRY({e})', end='', flush=True)
            time.sleep(3)
            continue
        if not candles:
            break
        added = 0
        for c in candles:
            if c[0] not in seen:
                seen.add(c[0])
                all_candles.append(c)
                added += 1
        if added == 0:
            break
        # Always advance by exactly 1000 candle-widths to force forward progress
        cur = candles[-1][0] + tf_ms
        time.sleep(0.35)

    all_candles.sort(key=lambda x: x[0])
    print(f' {len(all_candles)} свечей')
    with open(cache_file, 'wb') as f:
        pickle.dump(all_candles, f)
    return all_candles


def to_df(candles):
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    return df


def load_all_data():
    exchange = get_exchange()
    since = int((datetime.now(timezone.utc) - timedelta(days=MONTHS_BACK * 30)).timestamp() * 1000)

    data_1h = {}
    data_5m = {}

    print(f'\n=== Data loading: {len(BACKTEST_PAIRS)} pairs, {MONTHS_BACK} months ===')
    for i, pair in enumerate(BACKTEST_PAIRS):
        raw_1h = fetch_ohlcv_full(exchange, pair, '1h', since, label=f'{i+1}/{len(BACKTEST_PAIRS)}')
        raw_5m = fetch_ohlcv_full(exchange, pair, '5m', since, label=f'{i+1}/{len(BACKTEST_PAIRS)}')
        raw_4h = fetch_ohlcv_full(exchange, pair, '4h', since, label=f'{i+1}/{len(BACKTEST_PAIRS)}')
        data_1h[pair] = to_df(raw_1h)
        data_5m[pair] = to_df(raw_5m)
        data_1h[pair + '_4h'] = to_df(raw_4h)

    return data_1h, data_5m


# ── STRATEGY HELPERS (shared) ────────────────────────────────────────────────
def get_zones(setup):
    s, e, sz = setup['start_price'], setup['end_price'], setup['size']
    if setup['type'] == 'LONG':
        za = {'name': 'Zone_A', 'top': s + sz * ZONE_A_TOP,  'bottom': s + sz * ZONE_A_BOTTOM}
        zb = {'name': 'Zone_B', 'top': e - sz * ZONE_B_BOTTOM, 'bottom': e - sz * ZONE_B_TOP}
    else:
        za = {'name': 'Zone_A', 'top': s - sz * ZONE_A_BOTTOM, 'bottom': s - sz * ZONE_A_TOP}
        zb = {'name': 'Zone_B', 'top': e + sz * ZONE_B_TOP,   'bottom': e + sz * ZONE_B_BOTTOM}
    return za, zb


def price_in_zone(price, zone):
    return zone['bottom'] <= price <= zone['top']


def get_htf_trend(df_4h):
    if df_4h is None or len(df_4h) < 210:
        return 'NEUTRAL'
    close = df_4h['close']
    ema50  = close.ewm(span=50,  adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    lc, le50, le200 = close.iloc[-1], ema50.iloc[-1], ema200.iloc[-1]
    if lc > le50 and le50 > le200:
        return 'BULLISH'
    if lc < le50 and le50 < le200:
        return 'BEARISH'
    return 'NEUTRAL'


def calc_trade_params(setup, trigger_price, zone_name, balance):
    s, e, sz = setup['start_price'], setup['end_price'], setup['size']
    if setup['type'] == 'LONG':
        sl = (s + sz * ZONE_A_BOTTOM - sz * SL_BUFFER) if zone_name == 'Zone_A' else (s - sz * SL_BUFFER)
        min_sl = trigger_price * MIN_SL_PCT
        if trigger_price - sl < min_sl:
            sl = trigger_price - min_sl
        tp1 = e + sz * TP_LEVELS[0]
        tp2 = e + sz * TP_LEVELS[1]
        tp3 = e + sz * TP_LEVELS[2]
        tp4 = e + sz * TP_LEVELS[3]
    else:
        sl = (s - sz * ZONE_A_BOTTOM + sz * SL_BUFFER) if zone_name == 'Zone_A' else (s + sz * SL_BUFFER)
        min_sl = trigger_price * MIN_SL_PCT
        if sl - trigger_price < min_sl:
            sl = trigger_price + min_sl
        tp1 = e - sz * TP_LEVELS[0]
        tp2 = e - sz * TP_LEVELS[1]
        tp3 = e - sz * TP_LEVELS[2]
        tp4 = e - sz * TP_LEVELS[3]

    sl_dist = abs(trigger_price - sl)
    rr = abs(tp1 - trigger_price) / sl_dist if sl_dist > 0 else 0
    if rr < MIN_RR:
        return None

    risk_usd   = balance * RISK_PCT / 100
    pos_size   = risk_usd / sl_dist

    return {
        'entry': trigger_price, 'stop_loss': sl,
        'tp': [tp1, tp2, tp3, tp4],
        'pos_size': pos_size, 'risk_usd': risk_usd,
        'rr': rr, 'sl_dist': sl_dist,
        'type': setup['type'],
    }


# ── OLD STRATEGY ─────────────────────────────────────────────────────────────
def find_impulse_old(df, lookback=48):
    if len(df) < lookback:
        return None
    seg = df.tail(lookback).reset_index(drop=True)
    max_i = seg['high'].idxmax()
    min_i = seg['low'].idxmin()
    mx, mn = seg.loc[max_i, 'high'], seg.loc[min_i, 'low']
    if min_i < max_i:
        return {'type': 'LONG',  'start_price': mn, 'end_price': mx, 'size': mx - mn}
    elif max_i < min_i:
        return {'type': 'SHORT', 'start_price': mx, 'end_price': mn, 'size': mx - mn}
    return None


def find_extremes(df, n):
    """Фрактальные экстремумы. IMP_SOFT_FRACTALS=True: строгое сравнение слева,
    нестрогое справа — первая из равных вершин остаётся фракталом (v1 strict
    слеп к двойным вершинам с точно равными high)."""
    highs, lows = [], []
    hs = df['high'].values
    ls = df['low'].values
    for i in range(n, len(df) - n):
        h = hs[i]
        if IMP_SOFT_FRACTALS:
            ok_h = all(h > hs[i-j] for j in range(1, n+1)) and all(h >= hs[i+j] for j in range(1, n+1))
        else:
            ok_h = all(h > hs[i-j] and h > hs[i+j] for j in range(1, n+1))
        if ok_h:
            highs.append({'index': i, 'price': h})
        l = ls[i]
        if IMP_SOFT_FRACTALS:
            ok_l = all(l < ls[i-j] for j in range(1, n+1)) and all(l <= ls[i+j] for j in range(1, n+1))
        else:
            ok_l = all(l < ls[i-j] and l < ls[i+j] for j in range(1, n+1))
        if ok_l:
            lows.append({'index': i, 'price': l})
    return highs, lows


def detect_bos(df_5m, setup, zones, n, window, body_filter=False, return_latest=False):
    """BoS detection. body_filter=True: свеча-тело > 30% диапазона.
    return_latest=True: возвращает ПОСЛЕДНИЙ BoS (не первый) + freshness 24 свечи."""
    if len(df_5m) < 10:
        return None
    highs, lows = find_extremes(df_5m, n)
    if not highs or not lows:
        return None

    start = max(0, len(df_5m) - window)
    zone_visited = False
    active_zone  = None
    bos_events   = []

    for i in range(start, len(df_5m)):
        c = df_5m.iloc[i]
        if not zone_visited:
            for z in zones:
                if price_in_zone(c['close'], z) or price_in_zone(c['low'], z) or price_in_zone(c['high'], z):
                    zone_visited = True
                    active_zone  = z
                    break
        if not zone_visited:
            continue

        if body_filter:
            body = abs(c['close'] - c['open'])
            rng  = c['high'] - c['low']
            if rng > 0 and body / rng < 0.30:
                continue

        if setup['type'] == 'LONG':
            prev = [h for h in highs if h['index'] < i]
            if prev and c['close'] > prev[-1]['price']:
                # Минимальный пробой 0.1%
                if body_filter and c['close'] - prev[-1]['price'] < prev[-1]['price'] * 0.001:
                    continue
                ev = {'trigger_price': c['close'], 'trigger_time': c['timestamp'],
                      'zone': active_zone['name'], '_idx': i}
                if not return_latest:
                    return ev
                bos_events.append(ev)
        else:
            prev = [l for l in lows if l['index'] < i]
            if prev and c['close'] < prev[-1]['price']:
                if body_filter and prev[-1]['price'] - c['close'] < prev[-1]['price'] * 0.001:
                    continue
                ev = {'trigger_price': c['close'], 'trigger_time': c['timestamp'],
                      'zone': active_zone['name'], '_idx': i}
                if not return_latest:
                    return ev
                bos_events.append(ev)

    if not bos_events:
        return None
    last = bos_events[-1]
    if return_latest and last['_idx'] < len(df_5m) - 24:
        return None  # устаревший BoS
    last.pop('_idx', None)
    return last


def old_signal(df_1h, df_5m, df_4h, balance):
    setup = find_impulse_old(df_1h)
    if not setup:
        return None
    size_pct = setup['size'] / setup['end_price'] * 100
    if size_pct < MIN_IMPULSE_PCT:
        return None

    za, zb = get_zones(setup)
    cur = df_1h.iloc[-1]['close']
    ep, sz = setup['end_price'], setup['size']
    if setup['type'] == 'LONG' and cur < ep - sz * ZONE_B_TOP:
        return None
    if setup['type'] == 'SHORT' and cur > ep + sz * ZONE_B_TOP:
        return None

    active_zone = next((z for z in [za, zb] if price_in_zone(cur, z)), None)
    if not active_zone:
        return None

    htf = get_htf_trend(df_4h)
    if htf == 'BULLISH' and setup['type'] == 'SHORT':
        return None
    if htf == 'BEARISH' and setup['type'] == 'LONG':
        return None

    bos = detect_bos(df_5m, setup, [za, zb], n=2, window=30)
    if not bos:
        return None

    params = calc_trade_params(setup, bos['trigger_price'], bos['zone'], balance)
    if not params:
        return None
    params['htf'] = htf
    params['trigger_time'] = bos.get('trigger_time', df_5m.iloc[-1]['timestamp'])
    return params


# ── NEW STRATEGY (W10 logic) ──────────────────────────────────────────────────
def find_impulse_new(df, lookback=48):
    """Swing-based impulse detection: finds most recent structural leg.
    Ручки v2.1: IMP_ANCHOR_SNAP / IMP_SOFT_FRACTALS / IMP_FRESH_FALLBACK /
    IMP_MAX_LEG_RETRACE (дефолты = поведение v1)."""
    if len(df) < lookback:
        return None
    seg = df.tail(lookback).reset_index(drop=True)
    sh = seg['high'].values
    sl_ = seg['low'].values

    def _snap(direction, s_i, e_i):
        """Доснэп якорей к истинным экстремумам: A — экстремум чуть раньше/внутри
        ноги, B — экстремум ноги и чуть после фрактала (V-дно без фрактала,
        равные вершины, экстремум в несфрактализованном хвосте)."""
        w = IMP_ANCHOR_SNAP
        s2 = max(0, s_i - w)
        if direction == 'LONG':
            a_i = s2 + int(sl_[s2:e_i].argmin())
            e2 = min(len(seg) - 1, e_i + w)
            b_i = a_i + 1 + int(sh[a_i + 1:e2 + 1].argmax())
            return a_i, b_i, sl_[a_i], sh[b_i]
        else:
            a_i = s2 + int(sh[s2:e_i].argmax())
            e2 = min(len(seg) - 1, e_i + w)
            b_i = a_i + 1 + int(sl_[a_i + 1:e2 + 1].argmin())
            return a_i, b_i, sh[a_i], sl_[b_i]

    def _build(direction, s_i, e_i, s_p, e_p):
        size = abs(e_p - s_p)
        if size <= 0:
            return None
        num = e_i - s_i + 1
        # W12: импульс, а не долгая торговля — лимит длительности и мин. скорость
        if num > MAX_IMPULSE_CANDLES:
            return None
        if (size / e_p * 100) / num < MIN_IMPULSE_VELOCITY:
            return None
        if num > 15:
            sub = seg.iloc[s_i:e_i + 1]
            d_cnt = ((sub['close'] > sub['open']).sum() if direction == 'LONG'
                     else (sub['close'] < sub['open']).sum())
            if d_cnt / num < 0.60:
                return None
        if IMP_MAX_LEG_RETRACE:
            # чистота ноги: макс. внутренний откат против направления <= доли размера
            leg_h, leg_l = sh[s_i:e_i + 1], sl_[s_i:e_i + 1]
            if direction == 'LONG':
                max_dd = (np.maximum.accumulate(leg_h) - leg_l).max()
            else:
                max_dd = (leg_h - np.minimum.accumulate(leg_l)).max()
            if max_dd > IMP_MAX_LEG_RETRACE * size:
                return None
        return {'type': direction, 'start_price': s_p, 'end_price': e_p, 'size': size}

    # Swing method: find_extremes with n=2
    highs, lows = find_extremes(seg, n=2)
    if highs and lows:
        lh, ll = highs[-1], lows[-1]
        if ll['index'] < lh['index']:
            direction, s_i, e_i, s_p, e_p = 'LONG',  ll['index'], lh['index'], ll['price'], lh['price']
        else:
            direction, s_i, e_i, s_p, e_p = 'SHORT', lh['index'], ll['index'], lh['price'], ll['price']
        if IMP_ANCHOR_SNAP:
            s_i, e_i, s_p, e_p = _snap(direction, s_i, e_i)
        # Freshness: B in last 24 candles
        if e_i >= len(seg) - 24:
            setup = _build(direction, s_i, e_i, s_p, e_p)
            if setup:
                return setup

    # Fallback: global max/min
    max_i = int(seg['high'].idxmax())
    min_i = int(seg['low'].idxmin())
    mx, mn = seg.loc[max_i, 'high'], seg.loc[min_i, 'low']
    if min_i < max_i:
        direction, s_i, e_i, s_p, e_p = 'LONG', min_i, max_i, mn, mx
    elif max_i < min_i:
        direction, s_i, e_i, s_p, e_p = 'SHORT', max_i, min_i, mx, mn
    else:
        return None
    if IMP_FRESH_FALLBACK and e_i < len(seg) - 24:
        return None   # протухший fallback-импульс (v1 торговал без проверки свежести)
    return _build(direction, s_i, e_i, s_p, e_p)


def new_signal(df_1h, df_5m, df_4h, balance):
    setup = find_impulse_new(df_1h)
    if not setup:
        return None
    size_pct = setup['size'] / setup['end_price'] * 100
    if size_pct < MIN_IMPULSE_PCT:
        return None

    za, zb = get_zones(setup)
    cur = df_1h.iloc[-1]['close']
    ep, sz = setup['end_price'], setup['size']
    if setup['type'] == 'LONG' and cur < ep - sz * ZONE_B_TOP:
        return None
    if setup['type'] == 'SHORT' and cur > ep + sz * ZONE_B_TOP:
        return None

    active_zone = next((z for z in [za, zb] if price_in_zone(cur, z)), None)
    if not active_zone:
        return None

    htf = get_htf_trend(df_4h)
    if htf == 'BULLISH' and setup['type'] == 'SHORT':
        return None
    if htf == 'BEARISH' and setup['type'] == 'LONG':
        return None

    # W10: n=3, window=60, body_filter=True, return_latest=True
    bos = detect_bos(df_5m, setup, [za, zb], n=3, window=60,
                     body_filter=True, return_latest=True)
    if not bos:
        return None

    # W10: SL_BUFFER=1%, MIN_RR=2.0
    sl_buf_old = SL_BUFFER
    min_rr_old = MIN_RR
    globals()['SL_BUFFER'] = 0.010
    globals()['MIN_RR']    = 2.0
    params = calc_trade_params(setup, bos['trigger_price'], bos['zone'], balance)
    globals()['SL_BUFFER'] = sl_buf_old
    globals()['MIN_RR']    = min_rr_old

    if not params:
        return None
    params['htf'] = htf
    params['trigger_time'] = bos.get('trigger_time', df_5m.iloc[-1]['timestamp'])
    return params


# ── TRADE SIMULATION ─────────────────────────────────────────────────────────
def simulate_trade(params, df_5m, entry_time):
    """Simulate trade outcome on subsequent 5M candles. Returns trade result dict."""
    is_long  = params['type'] == 'LONG'
    entry    = params['entry']
    sl       = params['stop_loss']
    tps      = params['tp']
    pos_size = params['pos_size']

    mask      = df_5m['timestamp'] > entry_time
    future    = df_5m[mask].reset_index(drop=True)

    remaining  = pos_size
    total_pnl  = 0.0
    tp_hit     = 0
    be_set     = False
    be_sl      = entry  # breakeven level

    for _, candle in future.iterrows():
        hi, lo = candle['high'], candle['low']

        # After TP1: move SL to breakeven
        if tp_hit >= 1 and not be_set:
            sl = be_sl  # entry price (simplified, no 0.02% offset in backtest)
            be_set = True

        # Current TP target
        tp_price = tps[tp_hit] if tp_hit < 4 else None

        # Check hits
        sl_hit = (is_long and lo <= sl) or (not is_long and hi >= sl)
        tp_reached = tp_price is not None and (
            (is_long and hi >= tp_price) or (not is_long and lo <= tp_price)
        )

        # Resolve ambiguity (both in same candle)
        if sl_hit and tp_reached:
            open_p = candle['open']
            dist_sl = abs(open_p - sl)
            dist_tp = abs(open_p - tp_price)
            if dist_sl <= dist_tp:
                tp_reached = False
            else:
                sl_hit = False

        if sl_hit:
            pnl = remaining * (sl - entry if is_long else entry - sl)
            total_pnl += pnl
            return {
                'exit_reason': f'SL_after_TP{tp_hit}' if tp_hit > 0 else 'SL',
                'pnl': total_pnl, 'tps_hit': tp_hit,
                'exit_time': candle['timestamp'],
            }

        if tp_reached:
            frac      = TP_FRACTIONS[tp_hit]
            close_sz  = remaining * frac
            pnl       = close_sz * (tp_price - entry if is_long else entry - tp_price)
            total_pnl += pnl
            remaining -= close_sz
            tp_hit    += 1
            if tp_hit == 4:
                return {
                    'exit_reason': 'TP4',
                    'pnl': total_pnl, 'tps_hit': 4,
                    'exit_time': candle['timestamp'],
                }

    # Timeout — close at last available price
    last = future.iloc[-1] if len(future) > 0 else None
    if last is not None:
        lp  = last['close']
        pnl = remaining * (lp - entry if is_long else entry - lp)
        total_pnl += pnl
    return {
        'exit_reason': 'TIMEOUT',
        'pnl': total_pnl, 'tps_hit': tp_hit,
        'exit_time': future.iloc[-1]['timestamp'] if len(future) > 0 else entry_time,
    }


# ── BACKTEST RUNNER ──────────────────────────────────────────────────────────
def run_backtest(strategy_fn, data_1h, data_5m, label=''):
    print(f'\n[{label}] Scanning signals...', flush=True)

    all_signals = []  # (time, pair, params, df_5m_ref, entry_time)

    for pair in BACKTEST_PAIRS:
        df1  = data_1h[pair]
        df5  = data_5m[pair]
        df4h = data_1h.get(pair + '_4h')

        for i in range(LOOKBACK_1H + 10, len(df1)):
            df1_window = df1.iloc[i - LOOKBACK_1H: i + 1]
            cur_time   = df1_window.iloc[-1]['timestamp']

            # Get 5M data ending at cur_time (last 120 candles = 10 hours)
            mask5 = df5['timestamp'] <= cur_time
            df5_window = df5[mask5].tail(120)
            if len(df5_window) < 30:
                continue

            # Get 4H data ending at cur_time
            if df4h is not None:
                mask4 = df4h['timestamp'] <= cur_time
                df4h_window = df4h[mask4].tail(220)
            else:
                df4h_window = None

            params = strategy_fn(df1_window, df5_window, df4h_window, INITIAL_BALANCE)
            if params:
                # W10: используем время BoS-свечи, не конец окна
                entry_time = params.pop('trigger_time', df5_window.iloc[-1]['timestamp'])
                all_signals.append((entry_time, pair, params, df5, entry_time))

    print(f'[{label}] Signals found: {len(all_signals)} — simulating portfolio...', flush=True)

    # Sort by time and simulate portfolio
    all_signals.sort(key=lambda x: x[0])

    balance    = INITIAL_BALANCE
    trades     = []
    active     = {}   # pair → exit_time
    cooldown   = {}   # pair → exit_time
    balance_curve = [balance]

    for sig_time, pair, params, df5, entry_time in all_signals:
        # Skip if pair in cooldown or active
        if pair in active and active[pair] > sig_time:
            continue
        if pair in cooldown and cooldown[pair] + timedelta(hours=COOLDOWN_HOURS) > sig_time:
            continue

        # Skip if too many positions open
        open_count = sum(1 for et in active.values() if et > sig_time)
        if open_count >= MAX_POSITIONS:
            continue

        # Recalculate position size with current balance
        sl_dist = params['sl_dist']
        risk_usd = balance * RISK_PCT / 100
        pos_size = risk_usd / sl_dist
        params2  = dict(params)
        params2['pos_size'] = pos_size
        params2['risk_usd'] = risk_usd

        result = simulate_trade(params2, df5, entry_time)

        balance += result['pnl']
        balance_curve.append(balance)

        trade = {
            'pair':        pair,
            'entry_time':  entry_time,
            'exit_time':   result['exit_time'],
            'direction':   params['type'],
            'entry':       params['entry'],
            'sl':          params['stop_loss'],
            'rr':          round(params['rr'], 2),
            'exit_reason': result['exit_reason'],
            'tps_hit':     result['tps_hit'],
            'pnl':         result['pnl'],
            'pnl_pct':     result['pnl'] / (balance - result['pnl']) * 100,
            'balance':     balance,
        }
        trades.append(trade)
        active[pair]   = result['exit_time']
        cooldown[pair] = result['exit_time']

    return trades, balance_curve


# ── STATS & REPORT ───────────────────────────────────────────────────────────
def compute_stats(trades, balance_curve, label):
    if not trades:
        print(f'\n[{label}] No trades.')
        return {}

    df = pd.DataFrame(trades)
    n_total = len(df)
    n_win   = (df['pnl'] > 0).sum()
    n_loss  = (df['pnl'] < 0).sum()
    n_be    = n_total - n_win - n_loss
    win_rate = n_win / n_total * 100

    total_pnl     = df['pnl'].sum()
    total_pnl_pct = (balance_curve[-1] - balance_curve[0]) / balance_curve[0] * 100
    avg_win       = df[df['pnl'] > 0]['pnl'].mean() if n_win > 0 else 0
    avg_loss      = df[df['pnl'] < 0]['pnl'].mean() if n_loss > 0 else 0
    profit_factor = abs(df[df['pnl'] > 0]['pnl'].sum() / df[df['pnl'] < 0]['pnl'].sum()) if n_loss > 0 else math.inf

    # Max drawdown
    peak = balance_curve[0]
    max_dd = 0.0
    for b in balance_curve:
        if b > peak:
            peak = b
        dd = (peak - b) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Weekly trade count
    df['exit_time'] = pd.to_datetime(df['exit_time'])
    df['week'] = df['exit_time'].dt.to_period('W')
    trades_per_week = df.groupby('week').size().mean()

    # TP distribution
    tp_dist = df['tps_hit'].value_counts().sort_index().to_dict()

    # Exit reasons
    exit_dist = df['exit_reason'].value_counts().to_dict()

    # Avg RR
    avg_rr = df['rr'].mean()

    return {
        'label':           label,
        'n_total':         n_total,
        'n_win':           n_win,
        'n_loss':          n_loss,
        'n_be':            n_be,
        'win_rate':        win_rate,
        'total_pnl':       total_pnl,
        'total_pnl_pct':   total_pnl_pct,
        'final_balance':   balance_curve[-1],
        'avg_win':         avg_win,
        'avg_loss':        avg_loss,
        'profit_factor':   profit_factor,
        'max_drawdown':    max_dd,
        'trades_per_week': trades_per_week,
        'avg_rr':          avg_rr,
        'tp_dist':         tp_dist,
        'exit_dist':       exit_dist,
    }


def print_comparison(s_old, s_new):
    SEP = '-' * 62

    def fmt_pf(v):
        return f'{v:.2f}' if v != math.inf else 'inf'

    print('\n' + '=' * 62)
    print(f'  BACKTEST: {MONTHS_BACK} months | {len(BACKTEST_PAIRS)} pairs | Balance ${INITIAL_BALANCE:,.0f}')
    print('=' * 62)
    print(f'{"Metric":<28} {"OLD":>14} {"NEW":>14}')
    print(SEP)

    rows = [
        ('Total trades',          f"{s_old['n_total']}",           f"{s_new['n_total']}"),
        ('Trades/week',           f"{s_old['trades_per_week']:.1f}", f"{s_new['trades_per_week']:.1f}"),
        ('Wins / Losses',         f"{s_old['n_win']}/{s_old['n_loss']}", f"{s_new['n_win']}/{s_new['n_loss']}"),
        ('Win Rate',              f"{s_old['win_rate']:.1f}%",     f"{s_new['win_rate']:.1f}%"),
        ('Profit Factor',         fmt_pf(s_old['profit_factor']),  fmt_pf(s_new['profit_factor'])),
        ('Avg RR',                f"1:{s_old['avg_rr']:.2f}",      f"1:{s_new['avg_rr']:.2f}"),
        ('Avg Win ($)',           f"${s_old['avg_win']:.2f}",       f"${s_new['avg_win']:.2f}"),
        ('Avg Loss ($)',          f"${s_old['avg_loss']:.2f}",      f"${s_new['avg_loss']:.2f}"),
        ('Total PnL ($)',         f"${s_old['total_pnl']:+.2f}",   f"${s_new['total_pnl']:+.2f}"),
        ('Total PnL (%)',         f"{s_old['total_pnl_pct']:+.1f}%", f"{s_new['total_pnl_pct']:+.1f}%"),
        ('Final balance ($)',     f"${s_old['final_balance']:,.2f}", f"${s_new['final_balance']:,.2f}"),
        ('Max drawdown',          f"{s_old['max_drawdown']:.1f}%", f"{s_new['max_drawdown']:.1f}%"),
    ]

    for name, old_v, new_v in rows:
        print(f'{name:<28} {old_v:>14} {new_v:>14}')

    print(SEP)
    print('\n  TP distribution (trades per level):')
    print(f'  {"Level":<12} {"OLD":>10} {"NEW":>10}')
    for tp in range(5):
        if tp == 0:
            key = '0 TP (SL)'
        else:
            key = f'TP{tp}'
        old_c = s_old['tp_dist'].get(tp, 0)
        new_c = s_new['tp_dist'].get(tp, 0)
        print(f'  {key:<12} {old_c:>10} {new_c:>10}')

    print('\n  Exit reasons:')
    all_reasons = set(s_old['exit_dist']) | set(s_new['exit_dist'])
    for r in sorted(all_reasons):
        oc = s_old['exit_dist'].get(r, 0)
        nc = s_new['exit_dist'].get(r, 0)
        print(f'  {r:<20} {oc:>8} {nc:>8}')

    print('\n' + '=' * 62)
    delta_wr  = s_new['win_rate'] - s_old['win_rate']
    delta_pnl = s_new['total_pnl_pct'] - s_old['total_pnl_pct']
    delta_dd  = s_new['max_drawdown'] - s_old['max_drawdown']
    delta_n   = s_new['n_total'] - s_old['n_total']
    print('  DELTA (NEW vs OLD):')
    print(f'  WinRate: {delta_wr:+.1f}%  |  PnL: {delta_pnl:+.1f}%  |  DD: {delta_dd:+.1f}%  |  Trades: {delta_n:+d}')
    print('=' * 62 + '\n')


# ── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    t0 = time.time()

    data_1h, data_5m = load_all_data()

    trades_old, curve_old = run_backtest(old_signal, data_1h, data_5m, label='СТАРАЯ')
    trades_new, curve_new = run_backtest(new_signal, data_1h, data_5m, label='НОВАЯ')

    stats_old = compute_stats(trades_old, curve_old, 'СТАРАЯ')
    stats_new = compute_stats(trades_new, curve_new, 'НОВАЯ')

    print_comparison(stats_old, stats_new)

    # Save detailed trade logs
    if trades_old:
        pd.DataFrame(trades_old).to_csv(r'D:\Bot trade\backtest_old.csv', index=False)
    if trades_new:
        pd.DataFrame(trades_new).to_csv(r'D:\Bot trade\backtest_new.csv', index=False)
    print(f'Детальные логи: backtest_old.csv / backtest_new.csv')
    print(f'Время: {(time.time()-t0)/60:.1f} мин')
