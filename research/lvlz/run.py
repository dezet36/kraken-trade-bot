"""Прогон: события уровней по паре × заявки → итог каждой заявки.

    python -m lvlz.run --tag s1 --grid s1 [--src bybit] [--pairs ...] [--procs 4]

Пишет research/results/lvlz/{tag}/{PAIR}.pkl:
  {'ev':  {(tf, src, k, p): DataFrame событий с признаками},
   'res': {(tf, src, k, p, entry, target): DataFrame итогов по событиям}}
«Одна позиция на пару» — при разборе (lvlz.analyze), по варианту.
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
from lvlz import events as E

OUT_ROOT = os.path.join(D.ROOT, 'results', 'lvlz')

GRIDS = {
    # этап 1 — карта (протокол): ТФ × источник × прокол × вход × цель
    's1': dict(tfs=('1h', '4h', '12h', '1d'),
               sources=(('sw', 2), ('sw', 3), ('cl', 2), ('cl', 3), ('pd', 0), ('pw', 0)),
               p=(0.1, 0.3), entry=('mkt', 'lvl'), target=(2.0, 3.0, 5.0, 'opp', 'none'),
               E=(6,), H=(30,)),
    # этап 2 — область 8ч…1д (дополнение протокола 10.10)
    's2': dict(tfs=('8h', '12h', '1d'),
               sources=(('cl', 2), ('cl', 3), ('cl', 5), ('sw', 3), ('sw', 5)),
               p=(0.1, 0.3, 0.5), entry=('mkt', 'lvl'), target=('none', 5.0, 8.0),
               E=(3, 6, 12), H=(15, 30, 60), long_key=True),
}
ALLOWED = {'pd': ('1h', '4h'), 'pw': ('1h', '4h', '12h')}


def run_pair(args):
    pair, src, tag, grid = args
    out = os.path.join(OUT_ROOT, tag, f'{pair}.pkl')
    if os.path.exists(out):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, tfs=('1h', '4h', '8h', '12h', '1d'), src=src)
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'binance' if src == 'bv1h' else src, m1.t)
    evs, res = {}, {}
    for tf in grid['tfs']:
        if tf not in bars or bars[tf].tf < m1.tf:
            continue
        for (lsrc, k), p in itertools.product(grid['sources'], grid['p']):
            if lsrc in ALLOWED and tf not in ALLOWED[lsrc]:
                continue
            ev = E.build(bars, tf, lsrc, k, p)
            evs[(tf, lsrc, k, p)] = ev
            if ev.empty:
                continue
            for entry, tg, ex, hb in itertools.product(grid['entry'], grid['target'], grid['E'], grid['H']):
                if tg == 'opp' and lsrc not in ('pd', 'pw'):
                    continue
                if entry == 'mkt' and ex != grid['E'][0]:
                    continue                      # срок заявки у входа по рынку не значит ничего
                od = E.orders(ev, bars[tf], m1.tf, entry, tg, hb, expiry_bars=ex)
                v = od.pop('valid')
                odv = {kk: x[v] for kk, x in od.items()}
                if not len(odv['side']):
                    continue
                rr = sim.run(m1, odv, exclusive=False, cumf=cumf)
                key = (tf, lsrc, k, p, entry, tg, ex, hb) if grid.get('long_key') else (tf, lsrc, k, p, entry, tg)
                res[key] = pd.DataFrame({
                    'ev': rr['ev'], 'i_place': rr['i_place'],
                    'status': rr['status'].astype(np.int8), 'ex_type': rr['ex_type'].astype(np.int8),
                    'fill_i': rr['fill_i'], 'end_i': rr['end_i'],
                    'stop_pct': (rr['risk'] / rr['ref']).astype(np.float32),
                    'r_gross': rr['r_gross'].astype(np.float32),
                    'r_net': rr['r_net'].astype(np.float32),
                    'r_net15': (rr['r_gross'] - 1.5 * rr['fee_r'] - rr['fund_r']).astype(np.float32),
                })
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump({'ev': evs, 'res': res, 'funding': cumf is not None}, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out)
    return pair, f'{time.time() - t0:.0f} с, событий {sum(len(x) for x in evs.values())}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default='bybit')
    ap.add_argument('--tag', required=True)
    ap.add_argument('--grid', default='s1')
    ap.add_argument('--pairs', nargs='*')
    ap.add_argument('--procs', type=int, default=4)
    a = ap.parse_args()
    pairs = a.pairs or {'bybit': D.bybit_pairs, 'bv1h': D.bv_pairs}.get(a.src, lambda: D.PAIRS)()
    jobs = [(p, a.src, a.tag, GRIDS[a.grid]) for p in pairs]
    with Pool(a.procs) as pool:
        for pair, msg in pool.imap_unordered(run_pair, jobs):
            print(pair, msg, flush=True)


if __name__ == '__main__':
    main()
