"""
Старая ФИБО: проверка находки «медленный откат» — доза выдержки, 2025–26, пары
на 2021. Протокол — docs/ФИБО_разбор_сделок_2026-10-08.md, раздел 5
(закоммичен до расчёта, 6c95042).

    python research/fibo_slow_retrace.py   → results/fibo_slow_retrace.txt
"""
import copy
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fibo_live_sim as FS                            # noqa: E402
import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
import smc_engine                                     # noqa: E402
from common import ci                                 # noqa: E402
from fibo_crowd import funding_at                     # noqa: E402
from fibo_geometry import CELLS, impulse, load_cell  # noqa: E402
from fibo_htf_split import BASE                       # noqa: E402
from fibo_live_findings import naive                  # noqa: E402

H = np.timedelta64(1, 'h')
DELAYS = (2.0, 3.5, 6.0, 8.0)
BAD_PAIRS = {'BTCUSDT', 'UNIUSDT', 'ETHUSDT', 'LTCUSDT'}     # 5.3: R < −0.05 на 2022–24


def prepare(cell):
    cache, orders, pairs = load_cell(cell)
    orders = [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT]
    bt.CACHE_DIR = os.path.join(HERE, cache)
    fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in pairs}
    f3 = {}
    delayed = {dl: {} for dl in DELAYS}
    for p in pairs:
        own = [(i, o) for i, o in enumerate(orders) if o.pair == p]
        if not own:
            continue
        data = bt.load_pair(p)
        h1, m5 = data['1h'], data['5m']
        t1 = FS.naive(h1['timestamp'])
        hi1, lo1 = h1['high'].to_numpy(float), h1['low'].to_numpy(float)
        t5 = FS.naive(m5['timestamp'])
        hi5, lo5, c5 = (m5[c].to_numpy(float) for c in ('high', 'low', 'close'))
        for i, o in own:
            T = naive(o.created)
            d = 1 if o.direction == 'LONG' else -1
            f3[i] = o.direction == 'SHORT' and funding_at(fund.get(p), int(pd.Timestamp(T).value // 10 ** 6)) * d <= 0
            kt = int(np.searchsorted(t1 + H, T, side='right'))
            b, s, _, _ = impulse(o)
            ext = hi1 if d == 1 else lo1
            e_idx = next((m for m in range(kt - 1, max(-1, kt - 61), -1) if abs(ext[m] - b) <= 1e-9 * abs(b)), None)
            e_end = (t1[kt] + H if kt < len(t1) else T) if e_idx is None else t1[e_idx] + H
            a5 = int(np.searchsorted(t5, T, side='left'))
            for dl in DELAYS:
                start = e_end + np.timedelta64(int(dl * 60), 'm')
                if start <= T:
                    delayed[dl][i] = o
                    continue
                if start >= naive(o.expires):
                    continue
                b5 = int(np.searchsorted(t5, start, side='left'))
                tgt = o.targets[0]
                touched = ((lo5[a5:b5] <= o.entry).any() or (hi5[a5:b5] >= tgt).any()) if d == 1 else \
                          ((hi5[a5:b5] >= o.entry).any() or (lo5[a5:b5] <= tgt).any())
                if touched or b5 < 1:
                    continue
                price = c5[b5 - 1]
                lvl382 = b - d * 0.382 * s
                if not (min(lvl382, b) <= price <= max(lvl382, b)):
                    continue
                c = copy.copy(o)
                c.created = start
                delayed[dl][i] = c
    return cache, orders, pairs, f3, delayed


def run(pool, exec_data):
    out = {}
    for through in (0.0, 0.0005):
        smc_engine.FILL_THROUGH_PCT = through
        try:
            res = smc_engine.run_portfolio(pool, exec_data, **BASE)
        finally:
            smc_engine.FILL_THROUGH_PCT = 0.0
        out[through] = res['trades']
    return out


def run_cell(cell):
    cache, orders, pairs, f3, delayed = prepare(cell)
    exec_data = {p: d['5m'] for p in pairs if (d := bt.load_pair(p)) is not None}
    idx = [i for i in range(len(orders)) if f3.get(i)]
    pools = {'база F3': [orders[i] for i in idx]}
    for dl in DELAYS:
        pools[f'выдержка {dl:g} ч'] = [delayed[dl][i] for i in idx if i in delayed[dl]]
    kept = [i for i in idx if i in delayed[3.5]]
    gone = [i for i in idx if i not in delayed[3.5]]
    pools['(F3: заявки, которые Р1 убирает)'] = [orders[i] for i in gone]
    pools['(F3: заявки, которые Р1 оставляет, исходное время)'] = [orders[i] for i in kept]
    pools['F3 без BTC, UNI, ETH, LTC'] = [orders[i] for i in idx if orders[i].pair not in BAD_PAIRS]
    pools['(выдержка 3.5 ч и без этих пар)'] = [delayed[3.5][i] for i in kept if orders[i].pair not in BAD_PAIRS]
    out = {}
    for name, pool in pools.items():
        tr = run(pool, exec_data)
        out[name] = {k: pd.DataFrame({'pair': [t['pair'] for t in v], 'time': [t['entry_time'] for t in v],
                                      'r': [t['pnl'] / t['risk'] for t in v]}) for k, v in tr.items()}
    print(f'   {cell}: F3 {len(idx)}; ' + ', '.join(f'{dl:g} ч — {sum(1 for i in idx if i in delayed[dl])}'
                                                 for dl in DELAYS), flush=True)
    return cell, out


def m(df):
    return float(df.r.mean()) if len(df) else float('nan')


def main():
    with Pool(5) as pool:
        res = dict(pool.imap_unordered(run_cell, CELLS))
    names = list(res[CELLS[0]].keys())
    print('\n5.1 ДОЗА. R на сделку по ячейкам (касание), затем всё вместе: касание [95%] / насквозь')
    print(f'{"":52s} ' + ' '.join(f'{g[:3]}-{p:4s}' for g, p in CELLS))
    pooled = {}
    for n in names:
        a = pd.concat([res[c][n][0.0] for c in CELLS])
        t = pd.concat([res[c][n][0.0005] for c in CELLS])
        lo, hi = ci(a.r.to_numpy())
        pooled[n] = (m(a), m(t), lo)
        print(f'{n:52s} ' + ' '.join(f'{m(res[c][n][0.0]):+.3f}' for c in CELLS)
              + f'\n{"":52s} сделок {len(a)}, {m(a):+.3f} [{lo:+.3f}; {hi:+.3f}] | насквозь {m(t):+.3f}')
    print('\nПРИЁМКА ВЫДЕРЖКИ (сама: 8/9 лучше базы, насквозь > 0, нижняя > 0; сосед — R выше базы)')
    base = pooled['база F3'][0]
    for k, dl in enumerate(DELAYS):
        n = f'выдержка {dl:g} ч'
        better = sum(m(res[c][n][0.0]) > m(res[c]['база F3'][0.0]) for c in CELLS)
        own_ok = better >= 8 and pooled[n][1] > 0 and pooled[n][2] > 0
        nbrs = [f'выдержка {DELAYS[j]:g} ч' for j in (k - 1, k + 1) if 0 <= j < len(DELAYS)]
        nb_ok = any(pooled[x][0] > base for x in nbrs)
        print(f'  {n:14s} лучше в {better}/9; насквозь {pooled[n][1]:+.3f}; нижняя {pooled[n][2]:+.3f}; '
              f'сосед выше базы: {"да" if nb_ok else "нет"} → {"ПРИНЯТА" if own_ok and nb_ok else "не принята"}')

    print('\n5.2 2025–26: R тех, что Р1 убирает / оставляет (исходное время), по периодам')
    for per in ('bear', 'mid1', 'mid2', '12m'):
        g = pd.concat([res[c]['(F3: заявки, которые Р1 убирает)'][0.0] for c in CELLS if c[1] == per])
        k = pd.concat([res[c]['(F3: заявки, которые Р1 оставляет, исходное время)'][0.0] for c in CELLS if c[1] == per])
        d = pd.concat([res[c]['выдержка 3.5 ч'][0.0] for c in CELLS if c[1] == per])
        print(f'  {per:5s} убирает: {len(g)} сд {m(g):+.3f} | оставляет: {len(k)} сд {m(k):+.3f} | '
              f'после выдержки: {len(d)} сд {m(d):+.3f}')
    for n in ('база F3', 'выдержка 3.5 ч'):
        q = pd.concat([res[c][n][0.0] for c in CELLS if c[1] == '12m'])
        q['q'] = pd.to_datetime(q.time).dt.to_period('Q')
        print(f'  12m по кварталам, {n}: ' + ' '.join(f'{i} {g.r.mean():+.3f}({len(g)})' for i, g in q.groupby('q')))

    print('\n5.3 ПАРЫ на 2021 (решающая ячейка)')
    c = ('2021', 'y21')
    b, x = res[c]['база F3'], res[c]['F3 без BTC, UNI, ETH, LTC']
    print(f'  F3 все пары:         {len(b[0.0])} сд {m(b[0.0]):+.3f} | насквозь {m(b[0.0005]):+.3f}')
    print(f'  F3 без BTC/ETH/LTC:  {len(x[0.0])} сд {m(x[0.0]):+.3f} | насквозь {m(x[0.0005]):+.3f} → '
          f'{"ПРИНЯТО" if m(x[0.0]) > m(b[0.0]) and m(x[0.0005]) > 0 else "не принято"}')
    print('  по парам 2021:', b[0.0].groupby('pair').r.agg(['size', 'mean']).round(3).to_dict('index'))
    y = res[c]['(выдержка 3.5 ч и без этих пар)']
    print(f'  (выдержка 3.5 ч и без этих пар): {len(y[0.0])} сд {m(y[0.0]):+.3f} | насквозь {m(y[0.0005]):+.3f}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
