"""Разбор crowdz: карта DEV — R против толпы и контроль «с толпой», одна позиция на пару.

    python -m crowdz.analyze --tag c1 [--period DEV]
"""
from __future__ import annotations

import argparse
import glob
import os
import pickle

import numpy as np
import pandas as pd

from smcz import data as D
from smcz import sim
from lvlz.analyze import exclusive, stats, day_ci
from crowdz.run import OUT_ROOT


def load(tag):
    out = {}
    for path in sorted(glob.glob(os.path.join(OUT_ROOT, tag, '*.pkl'))):
        with open(path, 'rb') as f:
            out[os.path.basename(path)[:-4]] = pickle.load(f)
    return out


def frame(runs, key):
    parts = []
    for pair, run in runs.items():
        res = run['res'].get(key)
        if res is None or res.empty:
            continue
        ev = run['ev'][key[:3]]
        x = res.join(ev.drop(columns=['i_place', 'dir']), on='ev')
        x['dir'] = ev.dir.to_numpy()[x.ev.to_numpy()] * (1 if key[6] == 'against' else -1)
        x['pair'] = pair
        x['minute'] = x['t']
        parts.append(x)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df['period'] = D.period_of(df.minute.to_numpy())
    df['year'] = pd.to_datetime(df.minute * 60, unit='s').dt.year
    return df


def cmd_map(runs, period):
    keys = sorted({k for r in runs.values() for k in r['res'] if k[6] == 'against'}, key=str)
    rows = []
    for key in keys:
        df = exclusive(frame(runs, key).pipe(lambda x: x[x.period == period]).reset_index(drop=True))
        ctl = frame(runs, key[:6] + ('with',))
        ctl = exclusive(ctl[ctl.period == period].reset_index(drop=True)) if len(ctl) else ctl
        g, n_ = stats(df, 'r_gross'), stats(df, 'r_net')
        c_ = stats(ctl, 'r_net') if len(ctl) else dict(mean=np.nan)
        f = df[df.status == sim.ST_FILLED]
        rows.append(dict(def_=key[0], N=key[1], trig=key[2], stop=key[3], target=str(key[4]), hold=key[5],
                         n=n_['n'], gross=g['mean'], net=n_['mean'], t=n_['t'], with_crowd=c_['mean'],
                         edge=n_['mean'] - c_['mean'], stop_pct=f.stop_pct.median() * 100 if len(f) else np.nan,
                         win=(f.r_net > 0).mean() if len(f) else np.nan))
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', default='c1')
    ap.add_argument('--period', default='DEV')
    a = ap.parse_args()
    out = cmd_map(load(a.tag), a.period)
    out.to_csv(os.path.join(OUT_ROOT, f'{a.tag}_map_{a.period}.csv'), index=False)
    print(f'вариантов {len(out)}, net>0 {(out.net > 0).sum()}, t>=2 {(out.t >= 2).sum()}')


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
