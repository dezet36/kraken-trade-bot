"""
SMC без фандинга: разведка после провала заранее записанных гипотез
(30.09.2026, research/smc_improve.py — не принято ни одно из 22 правил).

ЭТО РАЗВЕДКА, А НЕ ПРИЁМКА. Правила ниже подсказаны разбивками
results/smc_stats.txt, то есть подобраны по тем же данным. Поэтому судит их
только то, что в подборе не участвовало: 23 пары кэша 12m вне списка бота
(раскладка 'live12x') — и то, держится ли эффект в ОБЕИХ половинах истории
по отдельности (2022-01 … 2024-06 и 2024-07 … 2026-09).

  V1 vol_high     волатильность пары в верхней четверти месяца (ранг ATR ≥ 75)
  V2 btc_strong   BTC за 7 дней ≥ +5% в сторону сделки
  V3 not_btc_mild BTC за 7 дней не в (−5%; 0) против сделки
  V4 stop_wide    стоп ≥ 0.25 дневного размаха
  P1 pair_prev    пара была в плюсе в ПРЕДЫДУЩЕМ периоде (устойчивость пар)
  F1 funding      фильтр толпы бота (ставка в сторону сделки ≤ −1 б.п.) — для
                  сравнения: работает ли он на всех 20 парах и на 23 новых
  S1 shorts       только шорты

Запуск (после smc_stats.py и smc_improve.py):
    python research/smc_explore.py > research/results/smc_explore.txt
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_improve as I                               # noqa: E402  (подменяет fresh на 20 пар)
from common import ci                                 # noqa: E402

E = I.E
EARLY = ('bear', 'mid1')
LATE = ('mid2', '12m', 'fresh')


def feat(frame, key):
    return frame['feats'].map(lambda x: (x or {}).get(key, np.nan))


def rules(frame):
    atr = feat(frame, 'atr_rank')
    btc = feat(frame, 'btc_ret_7d')
    stop_adr = feat(frame, 'stop_adr')
    fund = feat(frame, 'funding')
    return {
        'V1 vol_high': atr >= 75,
        'V2 btc_strong': btc >= 5,
        'V3 not_btc_mild': ~((btc > -5) & (btc < 0)),
        'V4 stop_wide': stop_adr >= 0.25,
        'F1 funding': ~(fund > -1.0),
        'S1 shorts': frame['dir'] == -1,
    }


def pair_prev(frame):
    """Пара в плюсе в предыдущем периоде (по заявкам отдельно). У первого периода — нет истории."""
    order = list(E.PERIODS)
    per = frame[frame['r'].notna()].groupby(['period', 'pair'])['r'].mean()
    out = []
    for period, pair in zip(frame['period'], frame['pair']):
        k = order.index(period)
        prev = per.get((order[k - 1], pair)) if k > 0 else None
        out.append(bool(prev is not None and prev > 0))
    return pd.Series(out, index=frame.index)


def cell(frame, mask, col='r'):
    part = frame.loc[mask & frame[col].notna(), col].to_numpy(dtype=float)
    base = frame.loc[frame[col].notna(), col].to_numpy(dtype=float)
    if len(part) < 10:
        return f'{len(part):4d} сд'
    lo, hi = ci(part)
    return f'{len(part):4d} сд {part.mean():+.3f} [{lo:+.3f}; {hi:+.3f}] (все {base.mean():+.3f})'


def main():
    pairs20 = tuple(I.D.PAIRS)
    full = I.build(E.PERIODS, 'live20', pairs20)
    x12 = I.build(('12m',), 'live12x', tuple(sorted(set(E.rows('12m', 'live12x')['pair']))))
    early = full['period'].isin(EARLY)
    late = full['period'].isin(LATE)
    r_full, r_x = rules(full), rules(x12)
    r_full['P1 pair_prev'] = pair_prev(full)
    print('R КАЖДОЙ ЗАЯВКИ ОТДЕЛЬНО (налив касанием; в скобках — все заявки той же выборки)')
    print(f'{"правило":16s} | {"2022-01…2024-06, 20 пар":44s} | {"2024-07…2026-09, 20 пар":44s} | 23 новые пары 12m')
    for name, mask in r_full.items():
        xm = r_x.get(name)
        right = cell(x12, xm) if xm is not None else '—'
        print(f'{name:16s} | {cell(full[early], mask[early]):44s} | {cell(full[late], mask[late]):44s} | {right}')
    print('\nто же при наливе «насквозь»')
    for name, mask in r_full.items():
        xm = r_x.get(name)
        right = cell(x12, xm, 'r_ft') if xm is not None else '—'
        print(f'{name:16s} | {cell(full[early], mask[early], "r_ft"):44s} | {cell(full[late], mask[late], "r_ft"):44s} | {right}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
