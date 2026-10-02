"""Прогон модели C (зона 4ч + CHoCH младшего ТФ) по парам.

    python -m smcz.run_model_c --src binance --tag mc1

Ключ итогов: (htf, k, ltf, k_ltf, zone, target, choch) — analyze.load берёт
его как структуру (htf, k) и геометрию (ltf, k_ltf, zone, target, choch).
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
from smcz import events as E
from smcz import model_c as MC
from smcz import sim

OUT_ROOT = os.path.join(D.ROOT, 'results', 'smcz')
HTF = [('4h', 3)]
LTF = [('15m', 2), ('15m', 3), ('1h', 2)]
ZONES = ['obw', 'obb']
TARGETS = [3.0, 8.0, 'none']
CHOCH = [False, True]
HOLD_H = 720


def _deadline(ev, B, t_sig, side):
    """Минутка закрытия бара первого встречного слома старшего ТФ после входа."""
    out = np.full(len(t_sig), len(B.i1) and B.i1[-1] - 1, np.int64)
    for s in (1, -1):
        opp = ev[ev.dir == -s]
        ot = opp.t.to_numpy()
        ob = opp.bar.to_numpy()
        m = side == s
        j = np.searchsorted(ot, t_sig[m], side='right')
        ok = j < len(ot)
        idx = np.flatnonzero(m)
        out[idx[ok]] = B.i1[ob[j[ok]]] - 1
    return out


def run_pair(args):
    pair, src, tag = args
    out = os.path.join(OUT_ROOT, tag, f'{pair}.pkl')
    if os.path.exists(out):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, src=src)
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'binance' if src == 'bv1h' else src, m1.t)
    res, evs = {}, {}
    for htf, k in HTF:
        ev = E.build(bars, htf, k)
        evs[(htf, k)] = ev
        B = bars[htf]
        for (ltf, kl), zone in itertools.product(LTF, ZONES):
            if bars[ltf].tf < m1.tf:
                continue
            sg = MC.signals(ev, bars, htf, ltf, kl, zone=zone)
            n = len(sg['ev'])
            if n == 0:
                continue
            dl = _deadline(ev, B, sg['t_sig'], sg['side'])
            for tg, ch in itertools.product(TARGETS, CHOCH):
                if tg == 'none':
                    tgt = np.where(sg['side'] == 1, np.inf, -np.inf)
                else:
                    tgt = sg['ref'] + sg['side'] * float(tg) * sg['risk']
                od = dict(side=sg['side'], i_place=sg['i_place'],
                          i_expire=sg['i_place'] + 1, entry=sg['entry'], stop=sg['stop'],
                          target=tgt, hold=np.full(n, HOLD_H * 60 // m1.tf),
                          ev=sg['ev'], ref=sg['ref'], risk=sg['risk'])
                if ch:
                    od['i_deadline'] = dl
                valid = sg['risk'] > 0
                odv = {kk: v[valid] for kk, v in od.items()}
                r = sim.run(m1, odv, exclusive=False, cumf=cumf)
                res[(htf, k, ltf, kl, zone, tg, ch)] = pd.DataFrame({
                    'ev': r['ev'], 'i_place': r['i_place'],
                    'status': r['status'].astype(np.int8),
                    'ex_type': r['ex_type'].astype(np.int8),
                    'fill_i': r['fill_i'], 'end_i': r['end_i'],
                    'stop_pct': (np.abs(r['ref'] - r['stop']) / r['ref']).astype(np.float32),
                    'rr': np.float32(np.nan),
                    'r_gross': r['r_gross'].astype(np.float32),
                    'r_net': r['r_net'].astype(np.float32),
                    'r_net15': (r['r_gross'] - 1.5 * r['fee_r'] - r['fund_r']).astype(np.float32),
                    'fund_r': r['fund_r'].astype(np.float32),
                })
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump({'ev': evs, 'res': res, 't1': m1.t[0], 'unit': m1.tf}, f,
                    protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out)
    return pair, f'{time.time() - t0:.0f} с'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default='binance')
    ap.add_argument('--tag', required=True)
    ap.add_argument('--pairs', nargs='*')
    ap.add_argument('--procs', type=int, default=3)
    a = ap.parse_args()
    pairs = a.pairs or {'bybit': D.bybit_pairs, 'bv1h': D.bv_pairs}.get(a.src, lambda: D.PAIRS)()
    with Pool(a.procs) as pool:
        for pair, msg in pool.imap_unordered(run_pair, [(p, a.src, a.tag) for p in pairs]):
            print(pair, msg, flush=True)


if __name__ == '__main__':
    main()
