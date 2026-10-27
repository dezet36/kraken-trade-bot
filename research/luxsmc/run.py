"""
Прогон LuxAlgo SMC как стратегии (PROTOCOL.md): разметка lux.build на 15м,
1ч, 4ч → заявки 54 вариантов → исполнение smcz.sim по минуткам Bybit с
фактическим фандингом. Итоги — по паре в results/luxsmc/<пара>.pkl, все
периоды; что из них видно — решает analyze.py.

    python research/luxsmc/run.py [--workers 2] [--pairs BTCUSDT ...]
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from smcz import data as D       # noqa: E402
from smcz import sim             # noqa: E402
import lux                       # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), 'results', 'luxsmc')
TFS = ('15m', '1h', '4h')
MODELS = ('M1', 'M2', 'M3')
TARGETS = ('T2', 'T3', 'TL')
ZONES = ('P0', 'P1')
STOP_BUF = 0.001
MIN_STOP = 0.0025
EXPIRY_BARS, HOLD_BARS = 24, 96
TL_MIN_R = 1.5


def orders(ev: pd.DataFrame, B, model, target, zone):
    if model == 'M3':
        x = ev[ev.scope == 'swing']
    else:
        x = ev[(ev.scope == 'internal') & (ev.side == ev.swing_bias)]
        if model == 'M2':
            x = x[x.tag == 'CHoCH']
    side = x.side.to_numpy(np.int64)
    long = side == 1
    entry = np.where(long, x.ob_hi, x.ob_lo).astype(np.float64)
    stop = np.where(long, x.ob_lo * (1 - STOP_BUF), x.ob_hi * (1 + STOP_BUF)).astype(np.float64)
    risk = np.abs(entry - stop)
    ok = risk / entry >= MIN_STOP
    top, bottom = x.trail_top.to_numpy(np.float64), x.trail_bottom.to_numpy(np.float64)
    if target == 'TL':
        tgt = np.where(long, top, bottom)
        with np.errstate(invalid='ignore'):
            ok &= ~np.isnan(tgt) & ((tgt - entry) * side >= TL_MIN_R * risk)
    else:
        tgt = entry + side * (2.0 if target == 'T2' else 3.0) * risk
    if zone == 'P1':
        mid = (top + bottom) / 2
        with np.errstate(invalid='ignore'):
            ok &= ~np.isnan(mid) & np.where(long, entry < mid, entry > mid)
    i_place = B.i1[x.i.to_numpy(np.int64)].astype(np.int64)
    sel = np.flatnonzero(ok)
    return {'side': side[sel], 'i_place': i_place[sel],
            'i_expire': i_place[sel] + EXPIRY_BARS * B.tf, 'entry': entry[sel],
            'stop': stop[sel], 'target': tgt[sel],
            'hold': np.full(len(sel), HOLD_BARS * B.tf, np.int64)}


def run_pair(pair):
    out = os.path.join(OUT, f'{pair}.pkl')
    if os.path.exists(out):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, tfs=TFS, src='bybit')
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    rows, counts = [], {}
    for tf in TFS:
        B = bars[tf]
        ev = pd.DataFrame(lux.build(B.h, B.l, B.c))
        counts[tf] = ev.groupby(['scope', 'tag']).size().to_dict() if len(ev) else {}
        if not len(ev):
            continue
        for model, target, zone in itertools.product(MODELS, TARGETS, ZONES):
            od = orders(ev, B, model, target, zone)
            if not len(od['side']):
                continue
            r = sim.run(m1, od, exclusive=True, cumf=cumf)
            take = r['keep'] & (r['status'] == sim.ST_FILLED)
            n_orders = int(r['keep'].sum())
            risk = np.abs(r['entry'] - r['stop'])
            rows.append(pd.DataFrame({
                'tf': tf, 'model': model, 'target': target, 'zone': zone,
                't': m1.t[r['i_place'][take]], 'side': r['side'][take].astype(np.int8),
                'ex_type': r['ex_type'][take].astype(np.int8),
                'r_net': r['r_net'][take].astype(np.float32),
                'r_net15': (r['r_gross'] - 1.5 * r['fee_r'] - r['fund_r'])[take].astype(np.float32),
                'stop_pct': (risk / r['entry'])[take].astype(np.float32),
                'orders': n_orders,
            }))
    res = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    res['pair'] = pair
    os.makedirs(OUT, exist_ok=True)
    tmp = out + '.tmp'
    pd.to_pickle({'trades': res, 'events': counts, 'funding': cumf is not None}, tmp)
    os.replace(tmp, out)
    return pair, f'{time.time() - t0:.0f} с, сделок {len(res)}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--pairs', nargs='*')
    a = ap.parse_args()
    pairs = a.pairs or D.bybit_pairs()
    with Pool(a.workers) as pool:
        for pair, msg in pool.imap_unordered(run_pair, pairs):
            print(pair, msg, flush=True)


if __name__ == '__main__':
    main()
