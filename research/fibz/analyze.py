"""Разбор прогонов fibz: «одна позиция на пару» по срезу, R до и после издержек.

    python -m fibz.analyze --tag s1 map            # этап 1: карта по ТФ/k/входу/цели (DEV)
    python -m fibz.analyze --tag s1 slices KEY...  # срезы по признакам для выбранных ключей
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
from fibz.run import OUT_ROOT

M1 = None


def load(tag):
    out = {}
    for path in sorted(glob.glob(os.path.join(OUT_ROOT, tag, '*.pkl'))):
        with open(path, 'rb') as f:
            out[os.path.basename(path)[:-4]] = pickle.load(f)
    return out


_t_cache = {}


def minute_t(pair, src='bybit'):
    """Минута (UTC) каждой минутки ряда пары — для периода по i_place."""
    if pair not in _t_cache:
        with np.load(os.path.join(D.BYBIT_DIR if src == 'bybit' else D.CACHE, f'{pair}_1m.npz')) as z:
            _t_cache[pair] = z['t']
    return _t_cache[pair]


def frame(runs, key, pair_src='bybit'):
    """Все заявки ключа по всем парам: итоги + признаки ноги + период."""
    parts = []
    tf, k, r, stp, tg, E, H = key
    for pair, run in runs.items():
        res = run['res'].get(key)
        if res is None or res.empty:
            continue
        lg = run['legs'][(tf, k, E)]
        x = res.join(lg.drop(columns=['i_place']), on='ev')
        x['pair'] = pair
        x['minute'] = minute_t(pair, pair_src)[np.minimum(x.i_place.to_numpy(), len(minute_t(pair, pair_src)) - 1)]
        parts.append(x)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df['period'] = D.period_of(df.minute.to_numpy())
    df['year'] = pd.to_datetime(df.minute * 60, unit='s').dt.year
    return df


def exclusive(df):
    """Одна заявка/позиция на пару внутри среза."""
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
        fill = (df.status == sim.ST_FILLED).mean() if len(df) else np.nan
        side = {s: stats(df[df.dir == s], 'r_net')['mean'] for s in (1, -1)}
        f = df[df.status == sim.ST_FILLED]
        rows.append(dict(tf=key[0], k=key[1], r=key[2], stop=key[3], target=str(key[4]), E=key[5], H=key[6],
                         n=n_['n'], fill=fill, gross=g['mean'], net=n_['mean'], t=n_['t'],
                         cost=g['mean'] - n_['mean'], long=side[1], short=side[-1],
                         rr=f.rr.median() if len(f) else np.nan,
                         stop_pct=f.stop_pct.median() * 100 if len(f) else np.nan,
                         win=(f.r_net > 0).mean() if len(f) else np.nan))
    out = pd.DataFrame(rows)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_rows', 500)
    if len(out) <= 300:
        print(f'Карта, {period}, Bybit, одна позиция на пару; R на сделку')
        print(out.round(3).to_string(index=False))
    return out


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


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
