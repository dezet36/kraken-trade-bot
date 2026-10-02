"""Итоговые числа замороженной модели C2 (SMCS) — всё, что идёт в отчёт.

    SMCZ_OPEN_HOLD=1 python -m smcz.report_final > results/smcz/final_C2.txt
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from smcz import analyze as A
from smcz import candidates as C
from smcz import data as D
from smcz import portfolio as P

POOL = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT', 'ADAUSDT',
        'AVAXUSDT', 'LINKUSDT', 'LTCUSDT', 'ZECUSDT', 'SUIUSDT', 'ARBUSDT', 'DOTUSDT',
        'XLMUSDT', 'SHIB1000USDT', 'NEARUSDT', 'UNIUSDT', 'AAVEUSDT', 'COTIUSDT', 'BICOUSDT']
PERIODS = ('DEV', 'VAL', 'HOLD')


def line(y, label):
    s = A.stats(y, ci=True)
    if not s['n']:
        return f'{label}: нет сделок'
    pp = y.groupby('pair').r_net.mean()
    return (f"{label}: n={s['n']:5d}  R {s['mean']:+.3f} [{s['lo']:+.3f}; {s['hi']:+.3f}]  "
            f"t={s['t']:.2f}  ×1.5 {y.r_net15.mean():+.3f}  win {s['win']:.2f}  "
            f"пар+ {(pp > 0).sum()}/{len(pp)}  long {y[y.dir == 1].r_net.mean():+.3f}  "
            f"short {y[y.dir == -1].r_net.mean():+.3f}  фандинг {y.fund_r.mean():+.3f}")


def block(title, x):
    print(f'\n== {title}')
    for p in PERIODS:
        print('  ', line(x[x.period == p], p))
    print('  ', line(x, 'ВСЕ'))
    yr = x.groupby(A.year(x)).r_net.agg(['size', 'mean']).round(3)
    print('   по годам:', {int(k): (int(v['size']), float(v['mean'])) for k, v in yr.iterrows()})


def portfolio(x, src, title):
    x = x.copy()
    x['t_in'] = x.t1 + x.fill_i
    x['t_out'] = x.t1 + x.end_i
    x['side'] = x.dir
    x['fill_px'] = x.close
    x['stop'] = np.where(x.dir == 1, x.close * (1 - x.stop_pct), x.close * (1 + x.stop_pct))
    closes = {}
    for p in x.pair.unique():
        b = D.load(p, src=src, tfs=('1d',))['1d']
        closes[p] = pd.Series(b.c, index=b.t // 1440)
    print(f'\n== портфель, {title}: риск 0.5% на сделку, капитал по дневной переоценке')
    for mss in (None, 8, 5):
        for per in ('ВСЕ', 'HOLD'):
            y = x if per == 'ВСЕ' else x[x.period == per]
            c = P.run_mtm(y, closes, risk=0.005, max_same_side=mss)
            eq = np.r_[10_000, c['eq'].to_numpy()]
            peak = np.maximum.accumulate(eq)
            dd = ((eq - peak) / peak).min()
            yrs = len(c) / 365.25
            s = c.set_index(pd.to_datetime(c.day * 86400, unit='s'))['eq']
            years = (s.groupby(s.index.year).last() / s.groupby(s.index.year).first() - 1).round(3)
            print(f"   в одну сторону ≤{mss or '∞'} {per:4s}: итог {eq[-1] / 1e4 - 1:+.1%}  "
                  f"в год {(eq[-1] / 1e4) ** (1 / yrs) - 1:+.1%}  просадка {dd:+.1%}"
                  + (f'  по годам {years.to_dict()}' if per == 'ВСЕ' else ''))


def main():
    _, by = C.check('by1', 'C2', PERIODS)
    _, bn = C.check('bn4', 'C2', PERIODS)
    block('Bybit, 31 пара', by)
    block('Bybit, пул бота (21 пара)', by[by.pair.isin(POOL)])
    block('Binance, 30 пар', bn)
    c = C.CANDS['C2']
    ev, res = A.load('bv1', c['struct'], geoms=[c['geom']])
    sym = ev.pair.str.partition('@')[0]
    today = set(D.PAIRS) | {'1000SHIBUSDT'}
    liq = (ev.adv30 >= 20e6) & (ev.age_d >= 90)
    base = np.asarray(c['mask'](ev), bool) & liq.to_numpy()
    bv = A.trades(ev, res[c['geom']], mask=base, reveal=PERIODS)
    block('широкая выборка Binance с умершими монетами (оборот ≥ 20 млн $)', bv)
    bvx = A.trades(ev, res[c['geom']], mask=base & ~sym.isin(today).to_numpy(), reveal=PERIODS)
    block('широкая выборка без сегодняшних 30 пар', bvx)
    portfolio(by[by.pair.isin(POOL)], 'bybit', 'пул бота (21 пара, Bybit)')
    portfolio(by, 'bybit', 'Bybit, 31 пара')


if __name__ == '__main__':
    main()
