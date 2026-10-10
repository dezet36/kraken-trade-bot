"""Прогон зон (дополнение протокола 10.10.2026): Z1 и Z2 × вход conf/edge × цель.

    python -m lvlz.zrun --tag z1 [--procs 4]

Ключ итога: (tf, kind, src, k, w, mode, target); признаки события — в 'ev'
под ключом (tf, kind, src, k, w, mode).
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
from lvlz import zones as Z
from lvlz.run import OUT_ROOT

H = 30
TFS = ('4h', '12h', '1d')
SHAPES = [('z1', 'sw', 3, 0.5), ('z1', 'sw', 3, 1.0), ('z1', 'cl', 3, 0.5), ('z1', 'cl', 3, 1.0),
          ('z2', '-', 0, 0.0)]
TARGETS = (3.0, 5.0, 'none')


def run_pair(args):
    pair, tag = args
    out = os.path.join(OUT_ROOT, tag, f'{pair}.pkl')
    if os.path.exists(out):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, tfs=('4h', '12h', '1d'), src='bybit')
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    evs, res = {}, {}
    for tf, (kind, src, k, w) in itertools.product(TFS, SHAPES):
        conf, edge = Z.build(bars, tf, kind, src, k, w)
        for mode, df in (('conf', conf), ('edge', edge)):
            evs[(tf, kind, src, k, w, mode)] = df
            if df.empty:
                continue
            for tg in TARGETS:
                od = Z.orders(df, bars[tf], m1.tf, mode, tg, H)
                v = od.pop('valid')
                odv = {kk: x[v] for kk, x in od.items()}
                if not len(odv['side']):
                    continue
                rr = sim.run(m1, odv, exclusive=False, cumf=cumf)
                res[(tf, kind, src, k, w, mode, tg)] = pd.DataFrame({
                    'ev': rr['ev'], 'i_place': rr['i_place'],
                    'status': rr['status'].astype(np.int8), 'ex_type': rr['ex_type'].astype(np.int8),
                    'fill_i': rr['fill_i'], 'end_i': rr['end_i'],
                    'stop_pct': (rr['risk'] / rr['ref']).astype(np.float32),
                    'r_gross': rr['r_gross'].astype(np.float32), 'r_net': rr['r_net'].astype(np.float32),
                    'r_net15': (rr['r_gross'] - 1.5 * rr['fee_r'] - rr['fund_r']).astype(np.float32),
                })
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump({'ev': evs, 'res': res}, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out)
    return pair, f'{time.time() - t0:.0f} с, событий {sum(len(x) for x in evs.values())}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', default='z1')
    ap.add_argument('--pairs', nargs='*')
    ap.add_argument('--procs', type=int, default=4)
    a = ap.parse_args()
    pairs = a.pairs or D.bybit_pairs()
    with Pool(a.procs) as pool:
        for pair, msg in pool.imap_unordered(run_pair, [(p, a.tag) for p in pairs]):
            print(pair, msg, flush=True)


if __name__ == '__main__':
    main()
