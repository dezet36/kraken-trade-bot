"""
Разбор прогона LuxAlgo SMC (PROTOCOL.md).

    python research/luxsmc/analyze.py dev                    # все 54 варианта на DEV + отбор
    python research/luxsmc/analyze.py val 1h/M1/T3/P0 ...    # приёмка отобранных на VAL
    LUX_OPEN_HOLD=1 python research/luxsmc/analyze.py hold 1h/M1/T3/P0   # один раз, с согласия владельца

HOLD без LUX_OPEN_HOLD=1 не читается вовсе: протокол держит машина.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from smcz import data as D                  # noqa: E402
from smcz.analyze import boot_ci            # noqa: E402

RES = os.path.join(os.path.dirname(HERE), 'results', 'luxsmc')
KEY = ['tf', 'model', 'target', 'zone']
MIN_DEV_TRADES = 200


def load(reveal):
    if 'HOLD' in reveal and os.getenv('LUX_OPEN_HOLD') != '1':
        raise SystemExit('HOLD закрыт до согласия владельца (LUX_OPEN_HOLD=1)')
    parts = []
    for f in sorted(os.listdir(RES)):
        if f.endswith('.pkl'):
            x = pd.read_pickle(os.path.join(RES, f))['trades']
            if len(x):
                x = x.assign(period=D.period_of(x.t.to_numpy()))
                parts.append(x[x.period.isin(reveal)])
    return pd.concat(parts, ignore_index=True)


def stats(x, col='r_net'):
    r = x[col].to_numpy(np.float64)
    n = len(r)
    if not n:
        return {'n': 0}
    return {'n': n, 'mean': r.mean(), 'sum': r.sum(), 'win': (r > 0).mean(),
            't': r.mean() / (r.std(ddof=1) / np.sqrt(n)) if n > 2 else np.nan,
            'mean15': x['r_net15'].mean(), 'stop_med': float(np.median(x.stop_pct)) * 100}


def name(k):
    return '/'.join(k)


def dev():
    x = load(('DEV',))
    rows = []
    for k, g in x.groupby(KEY):
        rows.append({'variant': name(k), **stats(g)})
    t = pd.DataFrame(rows).set_index('variant')
    pd.set_option('display.width', 200)
    print('DEV 2021–2023, все варианты (R на сделку после издержек):')
    print(t.sort_values('mean', ascending=False).round(3).to_string())
    # отбор: ≥ 200 сделок, не меньше половины соседей (тот же ТФ и модель) не в минусе
    t['tfm'] = [v.split('/')[0] + '/' + v.split('/')[1] for v in t.index]
    cands = []
    for v, row in t[t.n >= MIN_DEV_TRADES].sort_values('mean', ascending=False).iterrows():
        nb = t[(t.tfm == row.tfm) & (t.index != v)]
        share = (nb['mean'] >= 0).mean() if len(nb) else 0.0
        cands.append((v, row['mean'], row.n, share))
    print('\nкандидаты (≥ 200 сделок), доля соседей не в минусе:')
    for v, m, n, s in cands[:10]:
        print(f'  {v:16s} {m:+.3f}R  n={int(n)}  соседи {s:.0%}')
    chosen = [v for v, m, n, s in cands if m > 0 and s >= 0.5][:3]
    print('\nна VAL идут:', chosen or 'никто (нет варианта с плюсом и соседями)')
    x['variant'] = x.tf + '/' + x.model + '/' + x.target + '/' + x.zone
    x['year'] = pd.to_datetime(x.t * 60, unit='s', utc=True).dt.year
    for v in chosen:
        g = x[x.variant == v]
        print(f'  {v} по годам:', g.groupby('year').r_net.agg(['size', 'mean']).round(3).to_dict('index'))
    # для отчёта: лонги и шорты, средний стоп — по всем вариантам вместе
    print('\nвсе варианты вместе: лонги {:+.3f}R, шорты {:+.3f}R'.format(
        x[x.side == 1].r_net.mean(), x[x.side == -1].r_net.mean()))


def accept(periods, variants):
    x = load(periods)
    x['variant'] = x.tf + '/' + x.model + '/' + x.target + '/' + x.zone
    x['year'] = pd.to_datetime(x.t * 60, unit='s', utc=True).dt.year
    target = periods[-1]
    for v in variants:
        g = x[(x.variant == v) & (x.period == target)]
        s = stats(g)
        lo, hi = boot_ci(g.t.to_numpy(), g.r_net.to_numpy())
        pairs = g.groupby('pair').r_net.agg(['size', 'mean'])
        pairs = pairs[pairs['size'] >= 10]
        share = (pairs['mean'] > 0).mean() if len(pairs) else np.nan
        years = x[x.variant == v].groupby('year').r_net.mean()
        c1 = s['n'] and s['mean'] > 0 and (lo > 0 or (s['n'] >= 300 and s['t'] > 2 and lo >= 0))
        c2 = s['n'] and s['mean15'] > 0
        c3 = share >= 0.55
        c4 = bool((years >= -0.05).all())
        print(f'{v} на {target}: n={s["n"]} {s.get("mean", np.nan):+.3f}R [{lo:+.3f}; {hi:+.3f}] '
              f't={s.get("t", np.nan):.2f}, ×1.5 {s.get("mean15", np.nan):+.3f}, пар в плюсе {share:.0%}, '
              f'годы {years.round(3).to_dict()}')
        print(f'   1 {"да" if c1 else "нет"} · 2 {"да" if c2 else "нет"} · 3 {"да" if c3 else "нет"} · '
              f'4 {"да" if c4 else "нет"} → {"ПРИНЯТ" if c1 and c2 and c3 and c4 else "не принят"}')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'dev'
    if cmd == 'dev':
        dev()
    elif cmd == 'val':
        accept(('DEV', 'VAL'), sys.argv[2:])
    elif cmd == 'hold':
        accept(('DEV', 'VAL', 'HOLD'), sys.argv[2:])
