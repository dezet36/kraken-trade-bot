"""
Где SMC до издержек (docs/SMC_разбор_аналитика_2026-10-01.md, раздел 10):
заявки стенда «глазами бота» (results/smcr/live_orders.pkl, smcr_live.py build),
каждая отдельно, без двойного счёта 12m/fresh. R чистыми раскладывается на
комиссии, проскальзывание стопа и плоский фандинг; пересчёт при комиссиях ×0.5
и без комиссий — арифметика, не новая гипотеза.

    python research/smcr_costs.py   # → печать (results/smcr/eval_costs.txt)
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import smcr_eval as V  # noqa: E402

SLIPPAGE = 0.0005                                       # smc_engine.SLIPPAGE_PCT, стоп и выход по сроку
TAKER_EXITS = ('SL', 'TIME_STOP', 'EOD', 'BE')


def line(g, name):
    if len(g) < 5:
        return
    r, fee, slip, fund = g['r'], g['fees_r'], g['slip_r'], g['fund_r']
    print(f'  {name:30s} {len(g):5d} заявок | чистыми {r.mean():+.3f}R | комиссии {fee.mean():.3f}R | '
          f'проскальзывание ≈{slip.mean():.3f}R | фандинг {fund.mean():.3f}R | '
          f'до всех издержек ≈{(r + fee + slip + fund).mean():+.3f}R | комиссии ×0.5 {(r + 0.5 * fee).mean():+.3f}R | '
          f'без комиссий {(r + fee).mean():+.3f}R')


def main():
    f = pd.read_pickle(os.path.join(HERE, 'results', 'smcr', 'live_orders.pkl'))
    f = f[f['sample'].isin(['pool', 'other', 'x12']) & f['r'].notna()]
    f = f[(f['period'] != 'fresh') | (f['t'] > V.END_12M)].copy()
    f['slip_r'] = np.where(f['exit'].isin(TAKER_EXITS), SLIPPAGE / (f['stop_pct'] / 100), 0.0)
    f['fund_r'] = f['gross_r'] - f['r'] - f['fees_r']   # плоский фандинг стенда, R
    print(f'Заявок {len(f)}; стоп — медиана {f["stop_pct"].median():.2f}%; выходы: '
          + ', '.join(f'{k} {v:.0%}' for k, v in f['exit'].value_counts(normalize=True).items()))
    names = {'pool': '10 пар пула SMC', 'other': '10 пар списка вне пула', 'x12': '23 пары кэша 12m'}
    for sample in ('pool', 'other', 'x12'):
        g = f[f['sample'] == sample]
        print(f'\n== {names[sample]}')
        line(g, 'ядро (все сетапы)')
        if sample != 'x12':                             # у x12 нет ставки фандинга — фильтр не считается
            line(g[g['crowd']], 'с фильтром толпы (как в боте)')
            line(g[~g['crowd']], 'отсеянные фильтром')
        for p in ('bear', 'mid1', 'mid2', '12m', 'fresh'):
            h = g[g['period'] == p]
            line(h, f'  {p}: ядро')
            if sample != 'x12':
                line(h[h['crowd']], f'  {p}: с толпой')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
