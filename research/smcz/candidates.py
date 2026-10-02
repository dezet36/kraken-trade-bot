"""Кандидаты на VAL (PROTOCOL.md, дополнение 02.10.2026) и их проверка.

    python -m smcz.candidates --tag bn3 [--hold]   # --hold — только с SMCZ_OPEN_HOLD=1
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from smcz import analyze as A

CANDS = {
    'C1': dict(struct=('4h', 3), geom=('mkt', 'none', 6, 180, True),
               mask=lambda ev: (ev.kind == 0) & (ev.tr_1d == 1)),
    'C2': dict(struct=('4h', 3), geom=('mkt', 8.0, 6, 180, False),
               mask=lambda ev: ev.kind == 0),
    'C3': dict(struct=('8h', 3), geom=('mkt', 'none', 6, 90, True),
               mask=lambda ev: np.ones(len(ev), bool)),
    # модель C (зона 4ч + CHoCH 1ч) — прогоны smcz.run_model_c
    'C4': dict(struct=('4h', 3), geom=('1h', 2, 'obw', 8.0, True),
               mask=lambda ev: (ev.kind == 0) & (ev.tr_1d == 1), model='C'),
    'C5': dict(struct=('4h', 3), geom=('1h', 2, 'obw', 'none', True),
               mask=lambda ev: (ev.kind == 0) & (ev.tr_1d == 1), model='C'),
}


def check(tag, name, periods):
    c = CANDS[name]
    ev, res = A.load(tag, c['struct'], geoms=[c['geom']])
    x = A.trades(ev, res[c['geom']], mask=np.asarray(c['mask'](ev), bool), reveal=periods)
    out = {}
    for p in periods:
        y = x[x.period == p]
        s = A.stats(y, ci=True)
        s15 = A.stats(y, col='r_net15')
        pp = y.groupby('pair').r_net.agg(['size', 'mean'])
        pp = pp[pp['size'] >= 10]
        yr = y.groupby(A.year(y)).r_net.agg(['size', 'mean']).round(3)
        out[p] = dict(n=s['n'], mean=round(s['mean'], 3), lo=round(s['lo'], 3), hi=round(s['hi'], 3),
                      t=round(s['t'], 2), mean15=round(s15['mean'], 3),
                      pairs_pos=f"{(pp['mean'] > 0).sum()}/{len(pp)}",
                      long=round(y[y.dir == 1].r_net.mean(), 3), short=round(y[y.dir == -1].r_net.mean(), 3),
                      years={int(k): (int(v['size']), v['mean']) for k, v in yr.iterrows()})
    return out, x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', default='bn3')
    ap.add_argument('--hold', action='store_true')
    ap.add_argument('--only', nargs='*')
    a = ap.parse_args()
    periods = ('DEV', 'VAL', 'HOLD') if a.hold else ('DEV', 'VAL')
    for name in (a.only or CANDS):
        out, _ = check(a.tag, name, periods)
        print(name)
        for p, s in out.items():
            print('  ', p, s)


if __name__ == '__main__':
    main()
