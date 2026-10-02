"""Широкая выборка (Binance Vision, с умершими монетами): кандидаты C1–C3.

Отбор ликвидности — на дату события: средний дневной оборот Binance за 30
прошлых полных дней (adv30) и возраст ряда (age_d). Порог — до расчёта:
20 млн $ и 90 дней; чувствительность — 5 и 50 млн $.

    python -m smcz.broad --tag bv1 [--hold]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from smcz import analyze as A
from smcz import candidates as C
from smcz import data as D

THRESH = [5e6, 20e6, 50e6]
MIN_AGE = 90


def run(tag, names, periods):
    today = set(D.PAIRS) | {'1000SHIBUSDT'}
    for name in names:
        c = C.CANDS[name]
        ev, res = A.load(tag, c['struct'], geoms=[c['geom']])
        base = np.asarray(c['mask'](ev), bool)
        sym = ev.pair.str.partition('@')[0]
        for th in THRESH:
            liq = (ev.adv30 >= th) & (ev.age_d >= MIN_AGE)
            for label, extra in (('все', np.ones(len(ev), bool)),
                                 ('без 30 сегодняшних', ~sym.isin(today).to_numpy())):
                x = A.trades(ev, res[c['geom']], mask=base & liq.to_numpy() & extra, reveal=periods)
                parts = []
                for p in periods:
                    y = x[x.period == p]
                    s = A.stats(y, ci=True)
                    if not s['n']:
                        continue
                    yr = y.groupby(A.year(y)).r_net.mean().round(3).to_dict()
                    parts.append(f"{p}: n={s['n']} {s['mean']:+.3f} [{s['lo']:+.3f}; {s['hi']:+.3f}] "
                                 f"t={s['t']:.2f} пар={y.pair.nunique()} "
                                 f"long {y[y.dir == 1].r_net.mean():+.3f} short {y[y.dir == -1].r_net.mean():+.3f} "
                                 f"годы {yr}")
                print(f'{name} adv≥{th / 1e6:.0f}M {label}:')
                for s in parts:
                    print('    ', s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', default='bv1')
    ap.add_argument('--hold', action='store_true')
    ap.add_argument('--only', nargs='*')
    a = ap.parse_args()
    periods = ('DEV', 'VAL', 'HOLD') if a.hold else ('DEV', 'VAL')
    run(a.tag, a.only or ['C1', 'C2', 'C3'], periods)


if __name__ == '__main__':
    main()
