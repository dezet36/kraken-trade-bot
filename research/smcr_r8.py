"""
Разбор SMC, второй круг, R8: урезание риска SMC в трендовом режиме BTC
(smc/params.REGIME_RISK_SCALE = 0.5) — на сделках «глазами бота».

Режим — как у бота: market_regime.classify по 240 последним закрытым дневным
свечам BTC на момент заявки (strategy_smc.market_regime).

    python research/smcr_r8.py     # → results/smcr/eval_r8.txt
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Live_Bot'))

import smcr_eval as V                                 # noqa: E402
from common import ci                                 # noqa: E402

OUT = os.path.join(HERE, 'results', 'smcr')


def btc_daily():
    parts = [pd.read_pickle(os.path.join(HERE, f, 'BTCUSDT.pkl')) for f in ('flow_cache_2021', 'flow_cache')]
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep='last')].sort_index()
    d = df['c'].resample('1D').last().dropna()
    return d


def regime_at(daily, t_ms, cache={}):
    from analysis import market_regime as M
    day = pd.Timestamp(t_ms, unit='ms', tz='UTC').floor('D')
    if day in cache:
        return cache[day]
    closed = daily[daily.index < day]                 # только закрытые дни
    closes = closed.to_numpy()[-(M.ER_WINDOW + M.MIN_HISTORY + 30):]
    name, _er, _thr = M.classify(closes)
    cache[day] = name
    return name


def fmt(r):
    r = np.asarray([x for x in r if np.isfinite(x)], float)
    if len(r) < 3:
        return f'{len(r):3d} сд, итог {r.sum() if len(r) else 0:+.1f}R'
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    return f'{len(r):3d} сд {r.mean():+.3f} [{lo:+.3f}; {hi:+.3f}] итог {r.sum():+6.1f}R'


def main():
    from analysis import market_regime as M
    f = pd.read_pickle(os.path.join(OUT, 'live_orders_r2.pkl'))
    daily = btc_daily()
    f['regime'] = [regime_at(daily, int(t)) for t in f['t']]
    f['trend'] = f['regime'].isin([M.TREND_UP, M.TREND_DOWN])
    print('режим BTC на момент заявки:', f['regime'].value_counts().to_dict())
    for label, sel in (('живая SMC (фильтр толпы)', lambda d: d[d['crowd'] & d['fund_bp'].notna()]),
                       ('ядро без фильтра', lambda d: d)):
        print(f'\n{label}: сделки в тренде BTC (их бот ведёт с риском ×0.5) против боковика')
        for sample in ('pool', 'other'):
            g = sel(f[f['sample'] == sample])
            for lab, part in (('отбор bear', g[g['period'] == 'bear']), ('отбор mid1', g[g['period'] == 'mid1']),
                              ('приёмка', g[V.late_mask(g)])):
                tr = part[part['trend']]
                rg = part[~part['trend']]
                print(f'  {sample:5s} {lab:10s} тренд: {fmt(tr["r"].dropna())} | насквозь {tr["r_ft"].dropna().sum():+6.1f}R'
                      f' || боковик и прочее: {fmt(rg["r"].dropna())}')
        g = sel(f[f['sample'] == 'pool'])
        for lab, part in (('всё (без двойного счёта)', g[V.early_mask(g) | V.late_mask(g)]),):
            r = part['r'].fillna(0).to_numpy(float)
            w = np.where(part['trend'], 0.5, 1.0)
            print(f'  пул, {lab}: сумма R при риске ×1 {r.sum():+.1f}; с урезанием ×0.5 в тренде {(r * w).sum():+.1f}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
