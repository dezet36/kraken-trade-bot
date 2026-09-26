"""
Доктрина ИИ без модели: правила промта и ворота кода, исполненные механически.

ЗАЧЕМ. Модель нельзя прогнать по истории: один разбор идёт ~20 минут на
сервере, где работает бот. Но её план собирается по правилам, которые мы ей
дали и которые код проверяет воротами (docs/Промт_ИИ_v2.txt, llm_decide.check):

    сторона   — по структуре 4ч и дня (ворота «план против старшего тренда»);
    вход      — по живую сторону часового слома, в дисконте последней ноги;
    стоп      — за сломом структуры 1ч (последний HL / LH) плюс отступ
                охоты за стопами 0.3% + 0.1·ATR%; теснее минимума — отказ;
    цель      — ближняя нетронутая ликвидность с R:R ≥ 2 и не дальше
                дневного размаха («цель недостижима»);
    заявка    — 12 ч, снимается, если цена дошла до цели без входа;
    позиция   — безубыток при +1R, половина на первой цели, остаток на второй.

Если эти правила, исполненные без ошибок, не зарабатывают на истории, модели
нечего в них улучшать: ей нужен другой каркас. Если зарабатывают — модель
теряет на исполнении каркаса, и это видно сравнением с её живыми планами.

Структура — общий слой (smc.structure), без правок: build_structure один раз
на пару, решения только по свингам, подтверждённым к свече решения.

Запуск:
    python research/ai_doctrine.py                 # все периоды, все варианты
    python research/ai_doctrine.py mid1 doctrine   # один период, один вариант
"""

import bisect
import os
import pickle
import sys
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc import structure as S                     # noqa: E402

H = 3_600_000
M5 = 300_000

# Издержки — как у бумажного брокера (config.PAPER_*).
FEE_M, FEE_T, SLIP = 0.0002, 0.00055, 0.0005
BE_OFF = 0.00175                                   # config.BREAKEVEN_OFFSET_PCT

# Числа ворот ИИ (llm_decide / llm_context), не подбирались.
STOP_HUNT_BASE_PCT, STOP_HUNT_ATR_SHARE = 0.3, 0.1
MIN_STOP_COST_PCT, STOP_ATR_SHARE = 1.5, 0.5
MIN_RR = 2.0
TTL_H = 12
COOLDOWN_H = 4
MAX_HOLD_H = 336

PAIRS = ['XRPUSDT', 'ARBUSDT', 'SUIUSDT', 'ADAUSDT', 'UNIUSDT', 'DOGEUSDT', 'ETHUSDT',
         'BTCUSDT', 'SOLUSDT', 'ZECUSDT', 'AAVEUSDT', 'DOTUSDT', 'BNBUSDT', 'XLMUSDT',
         'LTCUSDT', 'LINKUSDT', 'AVAXUSDT', 'SHIB1000USDT', 'NEARUSDT', 'COTIUSDT']

PERIODS = {
    'bear': 'backtest_cache_bear',      # 2022-01 … 2023-06, BTC −35%
    'mid1': 'backtest_cache_mid1',      # 2023-07 … 2024-06, BTC +106%
    'mid2': 'backtest_cache_mid2',      # 2024-07 … 2025-04, BTC +50%
    '12m': 'backtest_cache_12m',        # 2025-05 … 2026-07, BTC −40%
    'fresh': 'backtest_cache_fresh',    # 2025-11 … 2026-09, BTC −24%
}

# Варианты: доктрина и по одному отступлению от неё.
BASE = {'side': '4h+1d', 'entry': 'eq', 'stop': '1h', 'tp': 'liq', 'be': True,
        'min_rr': MIN_RR, 'reach': True, 'sides': ('LONG', 'SHORT')}
VARIANTS = {
    'doctrine': {},
    'side_1h': {'side': '1h'},                 # сторона по часовому слому (как строила модель до 24.09)
    'side_4h': {'side': '4h'},                 # только ворота 4ч, дневной не нужен
    'no_side': {'side': 'none'},               # сторона — куда смотрит часовой слом, без старших
    'no_be': {'be': False},
    'tp_2r': {'tp': 2.0},                      # одна цель ровно 2R вместо ликвидности
    'tp_1r': {'tp': 1.0},
    'market': {'entry': 'market'},             # вход по рынку на закрытии часа, не лимитом в дисконте
    'stop_4h': {'stop': '4h'},                 # стоп за сломом 4ч
    'long_only': {'sides': ('LONG',)},
    'short_only': {'sides': ('SHORT',)},
    # Геометрия SMC (smc/params: MIN_RR 4, без безубытка, без предела по
    # дневному размаху) на входах доктрины — отличается ли каркас, у которого
    # есть плюсовой замер (research/results/broker_rules_smc_12m_close.txt).
    'rr4': {'min_rr': 4.0, 'be': False, 'reach': False},
    # Вход ПОСЛЕ выноса: часовая свеча проколола последний HL (LH) и закрылась
    # обратно — вход по закрытию, стоп за экстремумом прокола. Любимое условие
    # модели (sweep_reclaim, 42 плана из 91), но на уровне слома, а не где попало.
    'sweep': {'entry': 'sweep'},
    'sweep_1r': {'entry': 'sweep', 'min_rr': 1.0},
}


# ── Данные ───────────────────────────────────────────────────────────────────
def load(cache, pair, tf):
    path = os.path.join(ROOT, 'research', cache, f'{pair}_{tf}.pkl')
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        raw = pickle.load(fh)
    arr = np.array(sorted({r[0]: r for r in raw}.values()), dtype=float)
    return arr                                    # [ts, o, h, l, c, v]


def resample(arr, step):
    """Старший ТФ из часовых: свеча [ts, ts+step) целиком из часовых."""
    keys = (arr[:, 0] // step) * step
    out = []
    start = 0
    for k in range(1, len(arr) + 1):
        if k == len(arr) or keys[k] != keys[start]:
            seg = arr[start:k]
            if len(seg) == step // H:            # только полные свечи
                out.append([keys[start], seg[0, 1], seg[:, 2].max(), seg[:, 3].min(), seg[-1, 4], seg[:, 5].sum()])
            start = k
    return np.array(out, dtype=float)


def frame(arr):
    df = pd.DataFrame(arr[:, :6], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype('int64'), unit='ms')
    return df


def atr_pct(arr, n=14):
    h, l, c = arr[:, 2], arr[:, 3], arr[:, 4]
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(abs(h - prev), abs(l - prev)))
    atr = pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()
    return atr / c * 100


def day_range_pct(arr1h):
    """llm_market.day_range_pct: средний размах суток за 7 дней по часовым, % цены."""
    out = np.full(len(arr1h), np.nan)
    h, l, c = arr1h[:, 2], arr1h[:, 3], arr1h[:, 4]
    for i in range(24 * 7, len(arr1h)):
        spans = []
        base = i - 24 * 7 + 1
        for s in range(base, i - 22, 24):
            spans.append(h[s:s + 24].max() - l[s:s + 24].min())
        out[i] = np.mean(spans) / c[i] * 100
    return out


# ── Структура по мере подтверждения ──────────────────────────────────────────
class Tracker:
    """
    Состояние структуры одного ТФ на каждой свече — только из подтверждённого.

    Дублирует запросы smc.structure (state_at, invalidation_level, last_leg)
    инкрементально: те же точки, тот же порядок, без пересборки списков.
    """

    def __init__(self, arr):
        self.arr = arr
        self.s = S.build_structure(frame(arr))
        self.points = self.s['points']
        self.events = self.s['events']
        self.ev_idx = self.s['_event_index']
        self.ptr = 0
        self.last = {'HL': None, 'LH': None}
        self.last_kind = {'high': None, 'low': None}
        self.leg = None
        self.pending = []                          # свинги, ставшие видимыми на этом шаге

    def advance(self, index):
        """Все свинги, подтверждённые к свече index, опубликованы."""
        self.pending = []
        while self.ptr < len(self.points) and self.points[self.ptr]['confirmed_at'] <= index:
            p = self.points[self.ptr]
            if p['label'] in self.last:
                self.last[p['label']] = p
            other = self.last_kind['low' if p['kind'] == 'high' else 'high']
            self.leg = (other, p) if other is not None else None
            self.last_kind[p['kind']] = p
            self.pending.append(p)
            self.ptr += 1

    def trend(self, index):
        pos = bisect.bisect_right(self.ev_idx, index)
        return self.events[pos - 1]['direction'] if pos else S.NEUTRAL


class Liquidity:
    """Нетронутые экстремумы: видимые свинги, которые цена ещё не прошла."""

    def __init__(self):
        self.highs = []
        self.lows = []

    def add(self, point, after_high, after_low):
        if point['kind'] == 'high' and point['price'] > after_high:
            self.highs.append(point['price'])
        elif point['kind'] == 'low' and point['price'] < after_low:
            self.lows.append(point['price'])

    def take(self, high, low):
        self.highs = [p for p in self.highs if p > high]
        self.lows = [p for p in self.lows if p < low]


# ── План по правилам доктрины ────────────────────────────────────────────────
def plan_at(i, side, t1h, t4h, liq, v, arr1h, atr1, adr, extra_levels):
    """-> dict(entry, stop, targets, market) или (None, причина)."""
    long_ = side == 'LONG'
    close = arr1h[i, 4]
    want = S.BULLISH if long_ else S.BEARISH
    if t1h.trend(i) != want:
        return None, 'часовой слом не в сторону'
    inval = t1h.last['HL' if long_ else 'LH']
    if inval is None:
        return None, 'нет HL/LH'
    if (close <= inval['price']) if long_ else (close >= inval['price']):
        return None, 'цена за сломом'
    stop_ref = inval['price']
    if v['entry'] == 'sweep':
        low, high = arr1h[i, 3], arr1h[i, 2]
        pierced = low < inval['price'] if long_ else high > inval['price']
        if not pierced:
            return None, 'выноса нет'
        entry, market, end = close, True, inval['price']
        stop_ref = low if long_ else high
    else:
        leg = t1h.leg
        if leg is None or leg[1]['kind'] != ('high' if long_ else 'low'):
            return None, 'нога не по стороне'
        start, end = leg[0]['price'], leg[1]['price']
        eq = (start + end) / 2
        if v['entry'] == 'market':
            entry, market = close, True
        else:
            in_discount = close <= eq if long_ else close >= eq
            entry, market = (close, True) if in_discount else (eq, False)
    if v['stop'] == '4h':
        ref4 = t4h.last['HL' if long_ else 'LH']
        if ref4 is None:
            return None, 'нет HL/LH 4ч'
        stop_ref = ref4['price']
    a = atr1[i] if np.isfinite(atr1[i]) else 0.0
    buf = (STOP_HUNT_BASE_PCT + STOP_HUNT_ATR_SHARE * a) / 100
    stop = stop_ref * (1 - buf) if long_ else stop_ref * (1 + buf)
    if (stop >= entry) if long_ else (stop <= entry):
        return None, 'стоп за входом'
    risk = abs(entry - stop)
    stop_pct = risk / entry * 100
    floor = max(MIN_STOP_COST_PCT, STOP_ATR_SHARE * a)
    if stop_pct < floor:
        return None, 'стоп теснее минимума'
    if isinstance(v['tp'], float):
        targets = [entry + v['tp'] * risk if long_ else entry - v['tp'] * risk]
    else:
        pool = liq.highs if long_ else liq.lows
        pool = pool + [x for x in extra_levels if (x > entry if long_ else x < entry)]
        above = sorted({p for p in pool if (p > entry if long_ else p < entry)}, reverse=not long_)
        ok = [p for p in above if abs(p - entry) / risk >= v['min_rr']]
        if not ok:
            return None, 'нет цели с R:R'
        tp1 = ok[0]
        rest = [p for p in above if (p > tp1 if long_ else p < tp1)]
        targets = [tp1] + rest[:1]
    span = abs(targets[0] - entry) / entry * 100
    if v['reach'] and np.isfinite(adr[i]) and span > adr[i]:
        return None, 'цель дальше дневного размаха'
    to_entry = abs(close - entry) / close * 100
    to_target = abs(targets[0] - close) / close * 100
    if to_entry > 0.05 and to_target < to_entry:
        return None, 'цель ближе входа'
    return {'entry': entry, 'stop': stop, 'targets': targets, 'market': market,
            'stop_pct': stop_pct, 'leg_end': end}, ''


def side_allowed(v, trend1h, trend4h, trend1d):
    mode = v['side']
    if mode == '4h+1d':
        if trend4h == trend1d and trend4h in (S.BULLISH, S.BEARISH):
            return ['LONG' if trend4h == S.BULLISH else 'SHORT']
        return []
    if mode == '4h':
        return ['LONG'] if trend4h == S.BULLISH else ['SHORT'] if trend4h == S.BEARISH else []
    if mode in ('1h', 'none'):
        return ['LONG'] if trend1h == S.BULLISH else ['SHORT'] if trend1h == S.BEARISH else []
    return []


# ── Исполнение по 5m, как бумажный брокер ────────────────────────────────────
def execute(order, a5, t_order, v):
    ts5 = a5[:, 0]
    long_ = order['side'] == 'LONG'
    k = int(np.searchsorted(ts5, t_order))
    if k >= len(a5):
        return None
    tp1 = order['targets'][0]
    if order['market']:
        fill_k = k
        fill = a5[k, 1] * (1 + SLIP) if long_ else a5[k, 1] * (1 - SLIP)
        fee_in = FEE_T
    else:
        end = int(np.searchsorted(ts5, t_order + TTL_H * H))
        fill_k = None
        for j in range(k, min(end, len(a5))):
            hi, lo = a5[j, 2], a5[j, 3]
            if (lo <= order['entry']) if long_ else (hi >= order['entry']):
                fill_k = j
                break
            if (hi >= tp1) if long_ else (lo <= tp1):
                return {'status': 'цель до налива', 'end_t': a5[j, 0]}
        if fill_k is None:
            return {'status': 'не налилась', 'end_t': t_order + TTL_H * H}
        fill, fee_in = order['entry'], FEE_M
    stop = order['stop']
    if (fill <= stop) if long_ else (fill >= stop):
        return {'status': 'вход за стопом', 'end_t': a5[fill_k, 0]}
    risk = abs(fill - stop)
    targets = [t for t in order['targets'] if (t > fill if long_ else t < fill)]
    if not targets:
        return {'status': 'цель за входом', 'end_t': a5[fill_k, 0]}
    fractions = [0.5, 0.5] if len(targets) == 2 else [1.0]
    be_level = fill + risk if long_ else fill - risk
    sign = 1 if long_ else -1
    size, realized, fees = 1.0, 0.0, fee_in * fill
    cur, be_set, hit, mfe = stop, False, 0, 0.0
    last = min(len(a5), int(np.searchsorted(ts5, a5[fill_k, 0] + MAX_HOLD_H * H)))
    for j in range(fill_k, last):
        hi, lo = a5[j, 2], a5[j, 3]
        mfe = max(mfe, ((hi - fill) if long_ else (fill - lo)) / risk)
        if v['be'] and not be_set and ((hi >= be_level) if long_ else (lo <= be_level)):
            cur = fill * (1 + BE_OFF) if long_ else fill * (1 - BE_OFF)
            be_set = True
        if (lo <= cur) if long_ else (hi >= cur):
            px = cur * (1 - SLIP) if long_ else cur * (1 + SLIP)
            realized += sign * (px - fill) * size
            fees += FEE_T * px * size
            return {'status': 'вход', 'r': (realized - fees) / risk, 'reason': 'BE' if be_set else 'SL',
                    'fill_t': a5[fill_k, 0], 'end_t': a5[j, 0], 'mfe': mfe}
        while hit < len(targets):
            lvl = targets[hit]
            if not ((hi >= lvl) if long_ else (lo <= lvl)):
                break
            part = fractions[hit] if hit < len(targets) - 1 else size
            realized += sign * (lvl - fill) * part
            fees += FEE_M * lvl * part
            size -= part
            hit += 1
            if hit < len(targets):
                if not be_set:
                    cur, be_set = fill, True
            else:
                return {'status': 'вход', 'r': (realized - fees) / risk, 'reason': f'TP{hit}',
                        'fill_t': a5[fill_k, 0], 'end_t': a5[j, 0], 'mfe': mfe}
    j = last - 1
    px = a5[j, 4]
    realized += sign * (px - fill) * size
    fees += FEE_T * px * size
    return {'status': 'вход', 'r': (realized - fees) / risk, 'reason': 'TIME',
            'fill_t': a5[fill_k, 0], 'end_t': a5[j, 0], 'mfe': mfe}


# ── Прогон одной пары ────────────────────────────────────────────────────────
def prepare(cache, pair):
    a1 = load(cache, pair, '1h')
    a5 = load(cache, pair, '5m')
    if a1 is None or a5 is None or len(a1) < 24 * 60:
        return None
    a4 = resample(a1, 4 * H)
    ad = resample(a1, 24 * H)
    return {'pair': pair, 'a1': a1, 'a4': a4, 'ad': ad, 'a5': a5,
            'atr1': atr_pct(a1), 'adr': day_range_pct(a1)}


def run_pair(d, v, start_ms=None, end_ms=None):
    a1, a4, ad, a5 = d['a1'], d['a4'], d['ad'], d['a5']
    t1, t4, td = Tracker(a1), Tracker(a4), Tracker(ad)
    liq = Liquidity()
    trades, reasons = [], Counter()
    busy_until = -1
    seen_legs = set()
    j4 = jd = -1
    for i in range(len(a1)):
        t_close = a1[i, 0] + H
        t1.advance(i)
        for p in t1.pending:
            liq.add(p, a1[p['index'] + 1:i + 1, 2].max() if p['index'] < i else -np.inf,
                    a1[p['index'] + 1:i + 1, 3].min() if p['index'] < i else np.inf)
        while j4 + 1 < len(a4) and a4[j4 + 1, 0] + 4 * H <= t_close:
            j4 += 1
            t4.advance(j4)
            for p in t4.pending:
                since = int(np.searchsorted(a1[:, 0], a4[p['index'], 0] + 4 * H))
                liq.add(p, a1[since:i + 1, 2].max() if since <= i else -np.inf,
                        a1[since:i + 1, 3].min() if since <= i else np.inf)
        while jd + 1 < len(ad) and ad[jd + 1, 0] + 24 * H <= t_close:
            jd += 1
            td.advance(jd)
        liq.take(a1[i, 2], a1[i, 3])
        if i < 24 * 30 or j4 < 30 or jd < 30:
            continue
        if start_ms is not None and t_close < start_ms:
            continue
        if end_ms is not None and t_close >= end_ms:
            break
        if t_close < busy_until:
            continue
        sides = side_allowed(v, t1.trend(i), t4.trend(j4), td.trend(jd))
        sides = [s for s in sides if s in v['sides']]
        if not sides:
            reasons['сторона не разрешена'] += 1
            continue
        extra = [ad[jd, 2], ad[jd, 3]]            # вчерашние максимум и минимум
        for side in sides:
            plan, why = plan_at(i, side, t1, t4, liq, v, a1, d['atr1'], d['adr'], extra)
            if plan is None:
                reasons[why] += 1
                continue
            key = (side, round(plan['leg_end'], 10))
            if key in seen_legs:
                reasons['эта нога уже была'] += 1
                continue
            seen_legs.add(key)
            order = dict(plan, side=side, pair=d['pair'], t=t_close)
            res = execute(order, a5, t_close, v)
            if res is None:
                continue
            reasons[res['status']] += 1
            if res['status'] == 'вход':
                trades.append(dict(order, **res))
                busy_until = res['end_t'] + COOLDOWN_H * H
            else:
                busy_until = res['end_t']
            break
    return trades, reasons


def stats(trades):
    if not trades:
        return {'n': 0}
    r = np.array([t['r'] for t in trades])
    wins = r[r > 0.05].sum()
    loss = -r[r < -0.05].sum()
    eq = np.cumsum(r)
    dd = float((np.maximum.accumulate(eq) - eq).max()) if len(eq) else 0.0
    return {'n': len(r), 'sum': float(r.sum()), 'avg': float(r.mean()),
            'win': float((r > 0.05).mean() * 100), 'sl': sum(1 for t in trades if t['reason'] == 'SL'),
            'pf': float(wins / loss) if loss else float('inf'), 'dd': dd}


def fmt(s):
    if not s.get('n'):
        return 'сделок 0'
    return (f"сделок {s['n']:4d}  в плюс {s['win']:4.0f}%  стопов {s['sl'] / s['n'] * 100:3.0f}%  "
            f"сумма {s['sum']:+8.1f}R  в среднем {s['avg']:+.3f}R  PF {s['pf']:.2f}  просадка {s['dd']:.1f}R")


def run(period, variant_names, pairs=PAIRS, start_ms=None, end_ms=None, data=None, quiet=False):
    cache = PERIODS.get(period, period)
    loaded = data if data is not None else {}
    for pair in pairs:
        if pair not in loaded:
            d = prepare(cache, pair)
            if d is not None:
                loaded[pair] = d
    out = {}
    for name in variant_names:
        v = dict(BASE, **VARIANTS[name])
        trades, reasons = [], Counter()
        for pair, d in loaded.items():
            tr, rs = run_pair(d, v, start_ms, end_ms)
            trades += tr
            reasons.update(rs)
        out[name] = (trades, reasons)
        if not quiet:
            s = stats(trades)
            longs = stats([t for t in trades if t['side'] == 'LONG'])
            shorts = stats([t for t in trades if t['side'] == 'SHORT'])
            print(f'  {name:11s} {fmt(s)}')
            print(f'  {"":11s}   лонги: {fmt(longs)}')
            print(f'  {"":11s}   шорты: {fmt(shorts)}')
    return out, loaded


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    periods = [sys.argv[1]] if len(sys.argv) > 1 else list(PERIODS)
    names = sys.argv[2].split(',') if len(sys.argv) > 2 else list(VARIANTS)
    for period in periods:
        print(f'\n=== {period} ({PERIODS[period]}) ===')
        run(period, names)
