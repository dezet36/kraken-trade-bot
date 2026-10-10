"""Разбор прогонов lvlz: одна позиция на пару по варианту, R до и после издержек.

    python -m lvlz.analyze --tag s1 map [--period DEV]
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
from lvlz.run import OUT_ROOT


def load(tag):
    out = {}
    for path in sorted(glob.glob(os.path.join(OUT_ROOT, tag, '*.pkl'))):
        with open(path, 'rb') as f:
            out[os.path.basename(path)[:-4]] = pickle.load(f)
    return out


def frame(runs, key):
    """Все заявки варианта по всем парам: итоги + признаки события + период."""
    parts = []
    for pair, run in runs.items():
        res = run['res'].get(key)
        if res is None or res.empty:
            continue
        ev = run['ev'][key[:4]]
        x = res.join(ev.drop(columns=['i_place']), on='ev')
        x['pair'] = pair
        x['minute'] = x['t']
        parts.append(x)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df['period'] = D.period_of(df.minute.to_numpy())
    df['year'] = pd.to_datetime(df.minute * 60, unit='s').dt.year
    return df


def exclusive(df):
    """Одна заявка/позиция на пару внутри варианта."""
    keep = np.zeros(len(df), bool)
    for _, g in df.groupby('pair', sort=False):
        g = g.sort_values('i_place', kind='stable')
        kk = sim.one_at_a_time(g.i_place.to_numpy(np.int64), g.end_i.to_numpy(np.int64),
                               g.status.to_numpy(np.int64))
        keep[g.index[kk]] = True
    return df[keep]


def stats(df, col='r_net'):
    f = df[df.status == sim.ST_FILLED]
    x = f[col].to_numpy(float)
    n = len(x)
    if n < 2:
        return dict(n=n, mean=np.nan, t=np.nan)
    m = x.mean()
    return dict(n=n, mean=m, t=m / (x.std(ddof=1) / np.sqrt(n)))


def day_ci(df, col='r_net', reps=2000, seed=0):
    """95% бутстреп по дням."""
    f = df[df.status == sim.ST_FILLED]
    if len(f) < 10:
        return np.nan, np.nan
    day = (f.minute // 1440).to_numpy()
    u, inv = np.unique(day, return_inverse=True)
    s = np.bincount(inv, weights=f[col].to_numpy(float))
    c = np.bincount(inv)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(u), size=(reps, len(u)))
    means = s[idx].sum(1) / c[idx].sum(1)
    return np.percentile(means, 2.5), np.percentile(means, 97.5)


def cmd_map(runs, period='DEV'):
    keys = sorted({k for run in runs.values() for k in run['res']}, key=str)
    rows = []
    for key in keys:
        df = frame(runs, key)
        if df.empty:
            continue
        df = exclusive(df[df.period == period].reset_index(drop=True))
        g, n_ = stats(df, 'r_gross'), stats(df, 'r_net')
        f = df[df.status == sim.ST_FILLED]
        side = {s: stats(df[df.dir == s], 'r_net')['mean'] for s in (1, -1)}
        rows.append(dict(tf=key[0], src=key[1], k=key[2], p=key[3], entry=key[4], target=str(key[5]),
                         E=key[6] if len(key) > 6 else 6, H=key[7] if len(key) > 7 else 30,
                         n=n_['n'], fill=(df.status == sim.ST_FILLED).mean() if len(df) else np.nan,
                         gross=g['mean'], net=n_['mean'], t=n_['t'], cost=g['mean'] - n_['mean'],
                         long=side[1], short=side[-1],
                         stop_pct=f.stop_pct.median() * 100 if len(f) else np.nan,
                         win=(f.r_net > 0).mean() if len(f) else np.nan,
                         per_year=n_['n'] / 3.0))
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', required=True)
    ap.add_argument('cmd')
    ap.add_argument('--period', default='DEV')
    a = ap.parse_args()
    runs = load(a.tag)
    if a.cmd == 'map':
        out = cmd_map(runs, a.period)
        out.to_csv(os.path.join(OUT_ROOT, f'{a.tag}_map_{a.period}.csv'), index=False)
        pd.set_option('display.width', 250)
        pd.set_option('display.max_rows', 1000)
        print(f'Карта уровней, {a.period}, Bybit, одна позиция на пару; R на сделку')
        if len(out) <= 400:
            print(out.round(3).to_string(index=False))


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
