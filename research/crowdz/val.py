"""Проверка кандидатов V1–V3 (дополнение протокола 10.10.2026) на VAL (и HOLD с --hold).

    python -m crowdz.val [--period VAL]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from smcz import sim
from lvlz.analyze import exclusive, day_ci
from crowdz import analyze as C

V1 = ('l2', 1, 'turn', '3', 4.0, 10, 'against')
V2 = ('s5', 1, 'now', '1.5', 2.0, 10, 'against')


def trades(runs, keys, periods):
    parts = []
    for key in keys:
        x = C.frame(runs, key)
        x['leg'] = key[0]
        parts.append(x[x.period.isin(periods)])
    df = pd.concat(parts, ignore_index=True)
    return exclusive(df.reset_index(drop=True))


def report(name, df, period):
    f = df[df.status == sim.ST_FILLED]
    p = f[f.period == period]
    x = p.r_net.to_numpy(float)
    lo, hi = day_ci(df[df.period == period])
    t = x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if len(x) > 2 else np.nan
    pp = p.groupby('pair').r_net.agg(['size', 'mean'])
    pp = pp[pp['size'] >= 10]
    yrs = f.groupby('year').r_net.agg(['size', 'mean']).round(3)
    q = p.assign(q=pd.to_datetime(p.minute * 60, unit='s').dt.to_period('Q')).groupby('q').r_net.mean().round(3)
    print(f'\n== {name}, {period}: n {len(x)}, R {x.mean():+.3f} [{lo:+.3f}; {hi:+.3f}], t {t:.2f}, '
          f'×1.5 {p.r_net15.mean():+.3f}, пар в плюсе {(pp["mean"] > 0).sum()}/{len(pp)} '
          f'({(pp["mean"] > 0).mean() if len(pp) else np.nan:.0%}), выигрышей {(x > 0).mean():.0%}')
    print('   годы (все открытые периоды):', {int(k): (int(v['size']), float(v['mean'])) for k, v in yrs.iterrows()})
    print('   кварталы:', q.to_dict())
    print('   стороны:', p.groupby('dir').r_net.agg(['size', 'mean']).round(3).to_dict('index'))
    return dict(n=len(x), mean=x.mean(), lo=lo, t=t, m15=p.r_net15.mean(),
                pairs=(pp['mean'] > 0).mean() if len(pp) else np.nan,
                worst_year=yrs['mean'].min())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', default='VAL')
    a = ap.parse_args()
    runs = C.load('c1')
    opened = ('DEV', 'VAL') if a.period == 'VAL' else ('DEV', 'VAL', 'HOLD')
    for name, keys in (('V1 лонг', [V1]), ('V2 шорт', [V2]), ('V3 обе', [V1, V2])):
        r = report(name, trades(runs, keys, opened), a.period)
        if a.period == 'VAL':
            ok = [r['mean'] > 0 and (r['lo'] > 0 or (r['n'] >= 300 and r['t'] > 2)), r['m15'] > 0,
                  r['pairs'] >= 0.55, r['worst_year'] >= -0.05]
            print('   условия 1–4:', ok, '→', 'ПРОХОДИТ (до широкой выборки)' if all(ok) else 'не проходит')


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    main()
