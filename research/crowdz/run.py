"""Против толпы с нуля — события крайностей фандинга и заявки (research/crowdz/PROTOCOL.md).

    python -m crowdz.run --tag c1 [--procs 4] [--pairs ...]

Пишет research/results/crowdz/{tag}/{PAIR}.pkl:
  {'ev':  {(def, N, trig): DataFrame событий},
   'res': {(def, N, trig, stop, target, hold_d, ctrl): DataFrame итогов}}
ctrl — 'against' (против толпы, стратегия) или 'with' (та же минута и
геометрия, обратная сторона — контроль: есть ли у толпы направление).
"""
from __future__ import annotations

import argparse
import itertools
import os
import pickle
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

from smcz import data as D
from smcz import sim
from smcz import structure as S

OUT_ROOT = os.path.join(D.ROOT, 'results', 'crowdz')

# крайности: (имя, сторона против толпы, условие на ставку f и перцентили p5/p95)
DEFS = {
    's5':   (-1, lambda f, p5, p95: f >= 5),
    's10':  (-1, lambda f, p5, p95: f >= 10),
    's20':  (-1, lambda f, p5, p95: f >= 20),
    'sp95': (-1, lambda f, p5, p95: (f >= p95) & (f >= 3)),
    'l2':   (1, lambda f, p5, p95: f <= -2),
    'l5':   (1, lambda f, p5, p95: f <= -5),
    'l10':  (1, lambda f, p5, p95: f <= -10),
    'lp5':  (1, lambda f, p5, p95: (f <= p5) & (f <= -1)),
}
STREAK = (1, 3)
TRIG = ('now', 'turn')
STOPS = ('1.5', '3', 'swing')
TARGETS = (2.0, 4.0, 'none')
HOLD_D = (2, 5, 10)
TURN_BARS = 6
MIN_STOP = 0.005


def funding(pair):
    z = np.load(os.path.join(D.CACHE, 'funding_bybit', f'{pair}.npz'))
    t, r = z['t'].astype(np.int64), z['rate'].astype(float)
    order = np.argsort(t)
    t, r = t[order], r[order]
    gap_h = np.r_[8.0, np.diff(t) / 60.0]
    gap_h = np.where((gap_h > 0) & (gap_h <= 8), gap_h, 8.0)
    f = r * 1e4 * 8.0 / gap_h
    s = pd.Series(f, index=pd.to_datetime(t * 60, unit='s'))
    roll = s.rolling('90D', closed='left', min_periods=60)
    return t, f, roll.quantile(0.05).to_numpy(), roll.quantile(0.95).to_numpy()


def events(t, f, p5, p95, cond, n):
    """Выплаты, на которых условие выполнилось n раз подряд впервые после перерыва."""
    c = np.asarray(cond(f, p5, p95), bool) & np.isfinite(f)
    run = np.zeros(len(c), np.int64)
    for i in range(len(c)):
        run[i] = run[i - 1] + 1 if (c[i] and i > 0) else int(c[i])
    return np.flatnonzero(run == n)


def build(bars, t, f, p5, p95, dname, n, trig):
    d, cond = DEFS[dname]
    idx = events(t, f, p5, p95, cond, n)
    m1, B = bars['base'], bars['4h']
    atr = S.atr(B.h, B.l, B.c, 14)
    rows = []
    for k in idx:
        ts = t[k]
        if trig == 'now':
            j = int(np.searchsorted(B.tclose, ts, side='right')) - 1      # последний закрытый бар 4ч
            i0 = int(np.searchsorted(m1.t, ts, side='left'))
            if j < 6 or i0 <= 0 or i0 >= len(m1.t):
                continue
            ref = m1.c[i0 - 1]
        else:
            j0 = int(np.searchsorted(B.t, ts, side='left'))              # первый бар целиком после выплаты
            j = -1
            for q in range(j0, min(j0 + TURN_BARS, len(B))):
                if q < 1:
                    continue
                if (d == 1 and B.c[q] > B.h[q - 1]) or (d == -1 and B.c[q] < B.l[q - 1]):
                    j = q
                    break
            if j < 6:
                continue
            i0 = int(B.i1[j])
            if i0 >= len(m1.t):
                continue
            ref = B.c[j]
        a = atr[j]
        if not np.isfinite(a) or a <= 0:
            continue
        lo6, hi6 = B.l[j - 5:j + 1].min(), B.h[j - 5:j + 1].max()
        c7 = B.c[max(0, j - 42)]
        rows.append((d, ts, i0, ref, a, lo6, hi6, f[k], ref / c7 - 1))
    return pd.DataFrame(rows, columns=['dir', 't', 'i_place', 'ref', 'atr', 'lo6', 'hi6', 'f_bp', 'ret7d'])


def orders(ev, unit, stop, target, hold_d, ctrl):
    d = ev.dir.to_numpy() * (1 if ctrl == 'against' else -1)
    ref, a = ev.ref.to_numpy(), ev.atr.to_numpy()
    if stop == 'swing':
        st = np.where(d == 1, ev.lo6.to_numpy() - 0.1 * a, ev.hi6.to_numpy() + 0.1 * a)
    else:
        st = ref - d * float(stop) * a
    risk = (ref - st) * d
    tg = np.where(d == 1, np.inf, -np.inf) if target == 'none' else ref + d * float(target) * risk
    i_place = ev.i_place.to_numpy()
    hold = hold_d * 1440 // unit
    valid = (risk > 0) & (risk / ref >= MIN_STOP)
    return dict(side=d, i_place=i_place, i_expire=i_place + 1, entry=np.full(len(ev), np.nan), stop=st,
                target=tg, hold=np.full(len(ev), hold, np.int64), ev=np.arange(len(ev)), ref=ref,
                risk=risk), valid


def run_pair(args):
    pair, tag = args
    out = os.path.join(OUT_ROOT, tag, f'{pair}.pkl')
    if os.path.exists(out):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, tfs=('4h', '1d'), src='bybit')
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    t, f, p5, p95 = funding(pair)
    evs, res = {}, {}
    for dname, n, trig in itertools.product(DEFS, STREAK, TRIG):
        ev = build(bars, t, f, p5, p95, dname, n, trig)
        evs[(dname, n, trig)] = ev
        if ev.empty:
            continue
        for stop, tg, hd, ctrl in itertools.product(STOPS, TARGETS, HOLD_D, ('against', 'with')):
            od, v = orders(ev, m1.tf, stop, tg, hd, ctrl)
            odv = {kk: x[v] for kk, x in od.items()}
            if not len(odv['side']):
                continue
            rr = sim.run(m1, odv, exclusive=False, cumf=cumf)
            res[(dname, n, trig, stop, tg, hd, ctrl)] = pd.DataFrame({
                'ev': rr['ev'], 'i_place': rr['i_place'], 'status': rr['status'].astype(np.int8),
                'ex_type': rr['ex_type'].astype(np.int8), 'end_i': rr['end_i'],
                'stop_pct': (rr['risk'] / rr['ref']).astype(np.float32),
                'r_gross': rr['r_gross'].astype(np.float32), 'r_net': rr['r_net'].astype(np.float32),
                'r_net15': (rr['r_gross'] - 1.5 * rr['fee_r'] - rr['fund_r']).astype(np.float32)})
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'wb') as fh:
        pickle.dump({'ev': evs, 'res': res}, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out)
    return pair, f'{time.time() - t0:.0f} с, событий {sum(len(x) for x in evs.values())}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', default='c1')
    ap.add_argument('--pairs', nargs='*')
    ap.add_argument('--procs', type=int, default=4)
    a = ap.parse_args()
    pairs = a.pairs or D.bybit_pairs()
    with Pool(a.procs) as pool:
        for pair, msg in pool.imap_unordered(run_pair, [(p, a.tag) for p in pairs]):
            print(pair, msg, flush=True)


if __name__ == '__main__':
    main()
