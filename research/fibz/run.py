"""Прогон: ноги ФИБО по паре × геометрии заявок → итог каждой заявки.

    python -m fibz.run --tag s1 --grid s1 [--src bybit] [--pairs ...] [--procs 6]

Пишет research/results/fibz/{tag}/{PAIR}.pkl:
  {'legs': {(tf,k): DataFrame ног с признаками},
   'res':  {(tf,k,r,stop,target,E,H): DataFrame итогов по ногам}}
«Одна позиция на пару» здесь НЕ применяется — она зависит от среза и
делается при разборе (fibz.analyze).
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
from fibz import legs as LG

OUT_ROOT = os.path.join(D.ROOT, 'results', 'fibz')

GRIDS = {
    # этап 1 — карта: где у отката есть преимущество до и после издержек
    's1': dict(struct=[(tf, k) for tf in ('15m', '1h', '4h', '12h', '1d') for k in (2, 3, 5)],
               r=[0.382, 0.5, 0.618, 0.786], stop=['A'], target=['B', 'e1618', 2.0],
               E=[12], H=[120]),
    # этап 2 — поиск в области 6ч…1д (дополнение протокола 07.10)
    's2': dict(struct=[(tf, k) for tf in ('6h', '8h', '12h', '1d') for k in (2, 3, 5)],
               r=[0.236, 0.3, 0.382, 0.5], stop=['A', '786'],
               target=['e1272', 'e1618', 'e2618', 2.0, 3.0], E=[6, 12], H=[60, 120]),
}


def run_pair(args):
    pair, src, tag, grid = args
    out = os.path.join(OUT_ROOT, tag, f'{pair}.pkl')
    if os.path.exists(out):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, tfs=('15m', '1h', '4h', '6h', '8h', '12h', '1d'), src=src)
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'binance' if src == 'bv1h' else src, m1.t)
    legs, res = {}, {}
    for tf, k in grid['struct']:
        if tf not in bars or bars[tf].tf < m1.tf:
            continue
        for E in grid['E']:
            lg = LG.build(bars, tf, k, E)
            legs[(tf, k, E)] = lg
            if lg.empty:
                continue
            for r, stp, tg, H in itertools.product(grid['r'], grid['stop'], grid['target'], grid['H']):
                if stp == '786' and r >= 0.786:
                    continue
                od = LG.orders(lg, bars[tf], m1.tf, r, stp, tg, E, H)
                v = od.pop('valid')
                odv = {kk: x[v] for kk, x in od.items()}
                if not len(odv['side']):
                    continue
                rr = sim.run(m1, odv, exclusive=False, cumf=cumf)
                res[(tf, k, r, stp, tg, E, H)] = pd.DataFrame({
                    'ev': rr['ev'], 'i_place': rr['i_place'],
                    'status': rr['status'].astype(np.int8), 'ex_type': rr['ex_type'].astype(np.int8),
                    'fill_i': rr['fill_i'], 'end_i': rr['end_i'],
                    'rr': (np.abs(rr['target'] - rr['ref']) / rr['risk']).astype(np.float32),
                    'stop_pct': (rr['risk'] / rr['ref']).astype(np.float32),
                    'r_gross': rr['r_gross'].astype(np.float32),
                    'r_net': rr['r_net'].astype(np.float32),
                    'r_net15': (rr['r_gross'] - 1.5 * rr['fee_r'] - rr['fund_r']).astype(np.float32),
                })
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump({'legs': legs, 'res': res, 'funding': cumf is not None, 't': None},
                    f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out)
    return pair, f'{time.time() - t0:.0f} с, ног {sum(len(x) for x in legs.values())}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default='bybit')
    ap.add_argument('--tag', required=True)
    ap.add_argument('--grid', default='s1')
    ap.add_argument('--pairs', nargs='*')
    ap.add_argument('--procs', type=int, default=6)
    a = ap.parse_args()
    pairs = a.pairs or {'bybit': D.bybit_pairs, 'bv1h': D.bv_pairs}.get(a.src, lambda: D.PAIRS)()
    jobs = [(p, a.src, a.tag, GRIDS[a.grid]) for p in pairs]
    with Pool(a.procs) as pool:
        for pair, msg in pool.imap_unordered(run_pair, jobs):
            print(pair, msg, flush=True)


if __name__ == '__main__':
    main()
