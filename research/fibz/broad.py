"""Широкая выборка (Binance Vision, с умершими монетами) и сверка на Binance —
пункт 5 приёмки PROTOCOL.md: без сегодняшних пар R на DEV+VAL > 0.

    python -m fibz.broad bvc [bnc]
"""
import sys
import numpy as np, pandas as pd
from fibz import analyze as A
from fibz.val import CANDS
from smcz import data as D
from smcz import sim
sys.stdout.reconfigure(encoding='utf-8')
TODAY = set(D.PAIRS) | {'1000SHIBUSDT'}


def line(df, label):
    f = df[df.status == sim.ST_FILLED]
    if len(f) < 2:
        return f'    {label}: мало сделок'
    lo, hi = A.day_ci(df)
    st = A.stats(df)
    yr = f.groupby('year').r_net.mean().round(3).to_dict()
    return (f'    {label}: n {st["n"]} {st["mean"]:+.3f} [{lo:+.3f}; {hi:+.3f}] t {st["t"]:.2f} пар {f.pair.nunique()} '
            f'| лонг {f[f.dir==1].r_net.mean():+.3f} шорт {f[f.dir==-1].r_net.mean():+.3f} | годы {yr}')


def main(tag, broad=True):
    runs = A.load(tag)
    print(f'== {tag}: рядов {len(runs)}')
    for name, key in CANDS.items():
        full = A.frame(runs, key)
        if full.empty:
            continue
        sym = full.pair.str.partition('@')[0]
        print(f'{name} {key}')
        masks = {'все': np.ones(len(full), bool)}
        if broad:
            liq = ((full.adv30 >= 20e6) & (full.age_d >= 90)).to_numpy()
            masks = {'оборот ≥ 20 млн, все': liq, 'оборот ≥ 20 млн, без сегодняшних пар': liq & ~sym.isin(TODAY).to_numpy()}
        for label, m in masks.items():
            sub = full[m].reset_index(drop=True)
            for per in (('DEV',), ('VAL',), ('DEV', 'VAL')):
                df = A.exclusive(sub[sub.period.isin(per)].reset_index(drop=True))
                print(line(df, f'{label}, {"+".join(per)}'))


if __name__ == '__main__':
    main(sys.argv[1], broad=True)
    for t in sys.argv[2:]:
        main(t, broad=False)
