"""
SMC-свинг с нуля (01.10.2026, docs/SMC1_свинг_2026-10-01.md): дневная
ликвидность → снятие с возвратом → вход по рынку или по слому 4 ч от
имбаланса → стоп за экстремумом снятия → цель — неснятый дневной пул.
Свой код (примитивы research/smc0_engine.py), бот не используется.

    python research/smc1_swing.py     # → results/smc1/eval.txt
"""
import itertools
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc0_engine as E0                              # noqa: E402

OUT = os.path.join(HERE, 'results', 'smc1')
H_MS = 3_600_000
DAY_MS = 24 * H_MS
POOL20 = ['AAVEUSDT', 'ADAUSDT', 'ARBUSDT', 'AVAXUSDT', 'BNBUSDT', 'BTCUSDT', 'COTIUSDT', 'DOGEUSDT', 'DOTUSDT',
          'ETHUSDT', 'LINKUSDT', 'LTCUSDT', 'NEARUSDT', 'SHIB1000USDT', 'SOLUSDT', 'SUIUSDT', 'UNIUSDT', 'XLMUSDT',
          'XRPUSDT', 'ZECUSDT']
PERIODS = {'y21': ('2021-02-01', '2022-01-01'), 'dev': ('2022-01-01', '2024-07-01'),
           'val': ('2024-07-01', '2025-07-01'), 'hold': ('2025-07-01', '2026-09-28')}
BUF = 0.003
MIN_STOP = 0.015
MIN_RR, MAX_RR, FALLBACK_RR = 2.0, 8.0, 4.0
CONFIRM_H = 72
EXPIRY_LIMIT_H = 48
HOLD_H = 480
THRU = 0.0002


def load(pair):
    parts = []
    for f in ('flow_cache_2021', 'flow_cache', 'flow_cache_new', 'flow_cache_extra'):
        p = os.path.join(HERE, f, pair + '.pkl')
        if os.path.exists(p):
            parts.append(pd.read_pickle(p))
    if not parts:
        return None
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep='last')].sort_index()
    full = pd.date_range(df.index[0], df.index[-1], freq='1h')
    df = df.reindex(full)
    for c in ('o', 'h', 'l', 'c'):
        df[c] = df[c].ffill()
    df['v'] = df['v'].fillna(0.0)
    df['funding'] = df['funding'].ffill()
    return df


def funding_interval(df):
    """8 или 4 ч: в какие часы меняется выплаченная ставка."""
    f = df['funding']
    ch = f.ne(f.shift()) & f.notna() & f.shift().notna()
    hours = df.index.hour[ch.to_numpy()]
    if len(hours) < 20:
        return 8
    return 4 if np.mean(hours % 8 == 4) > 0.2 else 8


def resample(df, rule):
    out = df.resample(rule, label='left', closed='left').agg({'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last'})
    return out.dropna()


def daily_trend(h, l, c, k=2):
    """Направление последнего слома дневной структуры на закрытии каждого дня."""
    ph, pl = E0.pivots(h, l, k)
    n = len(c)
    trend = np.zeros(n, int)
    sh = sl = None
    cur = 0
    for t in range(n):
        s = t - k
        if s >= 0:
            if ph[s]:
                sh = h[s]
            if pl[s]:
                sl = l[s]
        if sh is not None and c[t] > sh:
            cur, sh = 1, None
        elif sl is not None and c[t] < sl:
            cur, sl = -1, None
        trend[t] = cur
    return trend


def daily_sweeps(d, k=2, min_age=3):
    """Снятия неснятых дневных свингов с возвратом в тот же или следующий день."""
    h, l, c = d['h'].to_numpy(float), d['l'].to_numpy(float), d['c'].to_numpy(float)
    ph, pl = E0.pivots(h, l, k)
    n = len(c)
    act_hi, act_lo = [], []
    pend = []                                   # [сторона, уровень, день прокола]
    out = []
    for t in range(n):
        s = t - k
        if s >= 0:
            if ph[s]:
                act_hi.append((h[s], s))
            if pl[s]:
                act_lo.append((l[s], s))
        # возврат после прокола вчера
        still = []
        for side, lv, b0 in pend:
            if (side > 0 and c[t] > lv) or (side < 0 and c[t] < lv):
                ext = min(l[b0], l[t]) if side > 0 else max(h[b0], h[t])
                out.append({'side': side, 't': t, 'b0': b0, 'level': lv, 'ext': ext})
        pend = still
        keep = []
        for lv, sb in act_lo:
            if l[t] < lv:
                if t - sb >= min_age:
                    if c[t] > lv:
                        out.append({'side': 1, 't': t, 'b0': t, 'level': lv, 'ext': l[t]})
                    else:
                        pend.append((1, lv, t))
            else:
                keep.append((lv, sb))
        act_lo = keep
        keep = []
        for lv, sb in act_hi:
            if h[t] > lv:
                if t - sb >= min_age:
                    if c[t] < lv:
                        out.append({'side': -1, 't': t, 'b0': t, 'level': lv, 'ext': h[t]})
                    else:
                        pend.append((-1, lv, t))
            else:
                keep.append((lv, sb))
        act_hi = keep
        for ev in out:
            if ev['t'] == t and 'opp' not in ev:
                ev['opp'] = sorted(p[0] for p in (act_hi if ev['side'] > 0 else act_lo))
    # один сетап на день и сторону — самый глубокий экстремум
    best = {}
    for ev in out:
        key = (ev['t'], ev['side'])
        if key not in best or (ev['ext'] - best[key]['ext']) * ev['side'] < 0:
            best[key] = ev
    return sorted(best.values(), key=lambda e: e['t'])


def setups(pair):
    df = load(pair)
    if df is None or len(df) < 24 * 200:
        return [], None, None
    d = resample(df, '1D')
    h4 = resample(df, '4h')
    trend = daily_trend(d['h'].to_numpy(float), d['l'].to_numpy(float), d['c'].to_numpy(float))
    d_ms = ((d.index - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy('int64')
    h4_ms = ((h4.index - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy('int64')
    h1_ms = ((df.index - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy('int64')
    h4h, h4l, h4c = h4['h'].to_numpy(float), h4['l'].to_numpy(float), h4['c'].to_numpy(float)
    ph4, pl4 = E0.pivots(h4h, h4l, 2)
    hi1, lo1, op1 = df['h'].to_numpy(float), df['l'].to_numpy(float), df['o'].to_numpy(float)
    fund = df['funding'].to_numpy(float)
    interval = funding_interval(df)
    norm = 8.0 / interval
    rows = []
    for ev in daily_sweeps(d):
        t = ev['t']
        side = ev['side']
        t_close = int(d_ms[t] + DAY_MS)                       # закрытие дня возврата
        ext = ev['ext']
        stop = ext * (1 - BUF) if side > 0 else ext * (1 + BUF)
        base = {'pair': pair, 'dir': side, 'aligned': trend[t] == side, 'level': ev['level'], 'stop': float(stop),
                'opp': ev.get('opp', [])}
        # (а) по рынку на открытии следующего дня
        j = int(np.searchsorted(h1_ms, t_close))
        if j < len(h1_ms):
            # момент решения — за миллисекунду до открытия дня: движок исполняет со свечи
            # ПОСЛЕ момента решения, и стоп-ордер по цене открытия наливается на ней же
            rows.append(dict(base, entry_mode='mkt', created=t_close - 1, t_close=t_close, entry=float(op1[j]),
                             etype='stop'))
        # (б) слом 4 ч в сторону сделки в течение 72 ч после закрытия дня, лимит от 4ч-имбаланса
        i_ext = int(np.searchsorted(h4_ms, d_ms[ev['b0']]))     # первый 4ч-бар дня прокола
        seg = slice(i_ext, int(np.searchsorted(h4_ms, d_ms[t] + DAY_MS)))
        if seg.stop > seg.start:
            if side > 0:
                i_low = seg.start + int(np.argmin(h4l[seg]))
            else:
                i_low = seg.start + int(np.argmax(h4h[seg]))
            u_end = int(np.searchsorted(h4_ms, t_close + CONFIRM_H * H_MS))
            ref = None
            for i in range(i_low, min(len(h4c), u_end)):
                s = i - 2
                if s >= i_low - 6 and s >= 0:
                    if side > 0 and ph4[s]:
                        ref = h4h[s]
                    if side < 0 and pl4[s]:
                        ref = h4l[s]
                if ref is None:
                    continue
                if (side > 0 and h4c[i] > ref) or (side < 0 and h4c[i] < ref):
                    gaps = E0.find_fvg(_S4(h4h, h4l), side, i_low + 1, i)
                    if gaps:
                        _j, lo, hi = gaps[-1]
                        entry = hi if side > 0 else lo
                        created = max(int(h4_ms[i] + 4 * H_MS), t_close)
                        k = int(np.searchsorted(h1_ms, created - H_MS, side='right')) - 1
                        price = float(df['c'].iloc[k])
                        if (price - entry) * side > 0:
                            rows.append(dict(base, entry_mode='4h', created=created, t_close=t_close, entry=float(entry),
                                             etype='limit'))
                    break
    for r in rows:
        k = int(np.searchsorted(h1_ms, r['created'] - H_MS, side='right')) - 1
        x = fund[k] * 1e4 * norm * r['dir'] if k >= 0 and np.isfinite(fund[k]) else np.nan
        r['crowd_ok'] = not (x > -1.0)
        risk = (r['entry'] - r['stop']) * r['dir']
        r['ok'] = risk > 0 and risk / r['entry'] >= MIN_STOP
        if r['ok']:
            # цель: неснятый дневной пул с другой стороны не ближе 2R, не тронутый до решения
            k0 = int(np.searchsorted(h1_ms, r['created'], side='right'))      # часы до решения
            k_t = int(np.searchsorted(h1_ms, r['t_close']))                   # от закрытия дня возврата
            seen_hi = hi1[k_t:k0].max() if k0 > k_t else -np.inf
            seen_lo = lo1[k_t:k0].min() if k0 > k_t else np.inf
            cands = [p for p in r['opp'] if (p - r['entry']) * r['dir'] >= MIN_RR * risk
                     and ((r['dir'] > 0 and p > seen_hi) or (r['dir'] < 0 and p < seen_lo))]
            tgt = (min(cands) if r['dir'] > 0 else max(cands)) if cands else r['entry'] + r['dir'] * FALLBACK_RR * risk
            if (tgt - r['entry']) * r['dir'] > MAX_RR * risk:
                tgt = r['entry'] + r['dir'] * MAX_RR * risk
            r['target'] = float(tgt)
            r['rr'] = abs(tgt - r['entry']) / risk
    rows = [r for r in rows if r['ok']]
    # исполнение — часовые свечи; фандинг — фактические выплаты
    exec_df = pd.DataFrame({'timestamp': df.index, 'open': df['o'].to_numpy(float), 'high': hi1, 'low': lo1,
                            'close': df['c'].to_numpy(float)})
    pay = (df.index.hour % interval == 0) & np.isfinite(fund)
    series = (h1_ms[pay], fund[pay])
    return rows, exec_df, series


class _S4:
    """Минимальный объект для E0.find_fvg (нужны только h и l)."""
    def __init__(self, h, l):
        self.h, self.l = h, l


def period_of(t_ms):
    for name, (a, b) in PERIODS.items():
        if int(pd.Timestamp(a, tz='UTC').value // 10 ** 6) <= t_ms < int(pd.Timestamp(b, tz='UTC').value // 10 ** 6):
            return name
    return None


def other_pairs():
    out = []
    for f in ('flow_cache_new', 'flow_cache_extra'):
        for n in sorted(os.listdir(os.path.join(HERE, f))):
            if n.endswith('.pkl') and n[:-4] not in POOL20 and n[:-4] not in out:
                out.append(n[:-4])
    return out


def main():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Live_Bot'))
    import smc_engine as SE
    from common import ci
    os.makedirs(OUT, exist_ok=True)
    data, fund, allrows = {}, {}, []
    for sample, pairs in (('pool', POOL20), ('other', other_pairs())):
        for pair in pairs:
            rows, exec_df, series = setups(pair)
            if exec_df is None:
                continue
            data[pair] = exec_df
            fund[pair] = SE.funding_series(*series)
            for r in rows:
                r['sample'] = sample
                r['period'] = period_of(r['created'])
            allrows += [r for r in rows if r['period'] is not None]
            print(f'  {sample} {pair}: сетапов {len(rows)}', flush=True)
    frame = pd.DataFrame(allrows)
    frame.drop(columns=['opp']).to_pickle(os.path.join(OUT, 'setups.pkl'))
    variants = list(itertools.product((True, False), ('mkt', '4h'), (False, True)))
    weeks = {name: (pd.Timestamp(b) - pd.Timestamp(a)).days / 7 for name, (a, b) in PERIODS.items()}
    print('\nпортфель (6 позиций, одна на пару), исполнение на часовых свечах, фандинг по факту')
    summary = []
    for aligned, mode, crowd in variants:
        label = f'{"по тренду" if aligned else "любое"} | {"рынок" if mode == "mkt" else "слом 4ч"} | ' \
                f'{"толпа" if crowd else "без толпы"}'
        res = {}
        for sample in ('pool', 'other'):
            f = frame[(frame['sample'] == sample) & (frame['entry_mode'] == mode)]
            if aligned:
                f = f[f['aligned']]
            if crowd:
                f = f[f['crowd_ok']]
            for per in PERIODS:
                g = f[f['period'] == per]
                orders = []
                for r in g.itertuples():
                    created = np.datetime64(int(r.created), 'ms')
                    expiry = 1.0 if r.etype == 'stop' else EXPIRY_LIMIT_H
                    orders.append(SE.Order(pair=r.pair, direction='BULLISH' if r.dir > 0 else 'BEARISH',
                                           entry=r.entry, stop=r.stop, targets=[r.target], fractions=[1.0],
                                           created=created, expires=created + np.timedelta64(int(expiry * 3600), 's'),
                                           key=(r.pair, int(r.created), r.dir), entry_type=r.etype,
                                           meta={'rr': r.rr}))
                pairs = sorted({o.pair for o in orders})
                SE.FILL_THROUGH_PCT = THRU
                SE.FUNDING_REAL = {p: fund[p] for p in pairs}
                try:
                    out = SE.run_portfolio(orders, {p: data[p] for p in pairs}, risk_pct=1.0, max_positions=6,
                                           cooldown_hours=0.0, breakeven_after_tp1=False, max_hold_hours=HOLD_H,
                                           max_same_direction=0, occupy_while_pending=True,
                                           cooldown_from_placement=False, cancel_at_target=False) if orders else {'trades': []}
                finally:
                    SE.FILL_THROUGH_PCT = 0.0
                    SE.FUNDING_REAL = None
                res[(sample, per)] = np.array([t['pnl'] / t['risk'] for t in out['trades']], dtype=float)
        dev = res[('pool', 'dev')]
        vh = np.concatenate([res[('pool', 'val')], res[('pool', 'hold')]])
        lo, hi = ci(vh) if len(vh) > 10 else (np.nan, np.nan)
        oth = np.concatenate([res[('other', p)] for p in PERIODS])
        per_wk = sum(len(res[('pool', p)]) for p in PERIODS) / sum(weeks.values())
        line = (f'  {label:30s} {per_wk:4.1f} сд/нед | dev {len(dev):3d} {dev.mean() if len(dev) else 0:+.3f} '
                f'| val {len(res[("pool", "val")]):3d} {res[("pool", "val")].mean() if len(res[("pool", "val")]) else 0:+.3f} '
                f'| hold {len(res[("pool", "hold")]):3d} {res[("pool", "hold")].mean() if len(res[("pool", "hold")]) else 0:+.3f} '
                f'| val+hold [{lo:+.3f}; {hi:+.3f}] | y21 {len(res[("pool", "y21")]):3d} '
                f'{res[("pool", "y21")].mean() if len(res[("pool", "y21")]) else 0:+.3f} '
                f'| вне пула {len(oth):4d} {oth.mean() if len(oth) else 0:+.3f} | итог пула '
                + ' '.join(f'{p} {res[("pool", p)].sum():+.1f}R' for p in PERIODS))
        print(line, flush=True)
        summary.append((dev.mean() if len(dev) else -9, label, res, lo))
    best = max(summary, key=lambda x: x[0])
    _, label, res, lo = best
    ok = all(res[('pool', p)].mean() > 0 for p in ('val', 'hold', 'y21') if len(res[('pool', p)])) \
        and np.concatenate([res[('other', p)] for p in PERIODS]).mean() > 0 and lo > 0
    print(f'\nЛучший по dev: {label} → {"ПРИБЫЛЬНА по протоколу" if ok else "НЕ проходит протокол"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
