"""
Разбор SMC, раздел 9: независимая проверка межтаймфреймовой связки 4ч→1ч на
2021 годе (2021-01…2021-12) — данные, которых отбор и приёмка не видели:
часовые свечи и фандинг research/flow_cache_2021 (2020-11 … 2022-01), 20 пар.
Исполнение — на ЧАСОВЫХ свечах (5-минуток за 2021 нет), консервативно: в одной
свече стоп раньше цели. Портфель с правилами брокера SMC, фандинг по факту.

    python research/smcr_mtf_2021.py     # → results/smcr/eval_mtf_2021.txt
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

import logger                                          # noqa: E402
logger.log = lambda *a, **k: None

import smcr_mtf as M                                  # noqa: E402

START = int(pd.Timestamp('2021-01-01', tz='UTC').value // 10 ** 6)
END = int(pd.Timestamp('2022-01-01', tz='UTC').value // 10 ** 6)


def load(pair):
    p = os.path.join(HERE, 'flow_cache_2021', pair + '.pkl')
    if not os.path.exists(p):
        return None
    f = pd.read_pickle(p).sort_index()
    f = f[~f.index.duplicated(keep='last')]
    if len(f) < 24 * 120:
        return None
    df = pd.DataFrame({'timestamp': f.index, 'open': f['o'].values, 'high': f['h'].values, 'low': f['l'].values,
                       'close': f['c'].values, 'volume': f['v'].values}).reset_index(drop=True)
    return df, f['funding'].to_numpy(float)


def setups(pair):
    import backtest_smc as bt
    from strategies.smc import imbalance, liquidity, signal as smc_signal, structure as structure_mod
    got = load(pair)
    if got is None:
        return [], None, None
    h1, fund = got
    h4 = bt.resample(h1, '4h')
    d1 = bt.resample(h1, '1D')
    ctx = smc_signal.build_context({'bias': d1, 'htf': h4, 'poi': h1}, pair=pair)
    s4 = structure_mod.build_structure(h4, tier='swing')
    pools4 = liquidity.find_liquidity_pools(h4, s4)
    L = M._frame(h4, 4 * M.H, s4, imbalance.find_fvg(h4), pools4, liquidity.find_sweeps(h4, pools4))
    E = M._frame(h1, M.H, ctx.structure, ctx.fvgs)
    ts1 = E['ts']
    rows = [r for r in M._setups('4ч→1ч', L, E, ctx, h1['timestamp'], ts1, 'y21', pair) if START <= r['t'] < END]
    # фандинг: ставка последней выплаты на момент решения (час до решения), б.п. в сторону сделки
    for r in rows:
        j = int(np.searchsorted(ts1, r['t'] - M.H, side='right')) - 1
        r['crowd_ok'] = not (fund[j] * 1e4 * r['dir'] > -1.0) if j >= 0 and np.isfinite(fund[j]) else True
    # выплаты в 00/08/16 UTC; дыры в ряду (у XRP 4648 часов без ставки) — без
    # выплаты, а не нулевая ставка и не последнее известное число
    pay_mask = (h1['timestamp'].dt.hour % 8 == 0).to_numpy() & np.isfinite(fund)
    series = (ts1[pay_mask], fund[pay_mask])
    return rows, h1, series


def main():
    import ai_doctrine as D
    import smc_lab_eval as E
    from common import ci
    pool10 = tuple(E.POOL10)
    other10 = tuple(p for p in D.PAIRS if p not in pool10)
    allrows, data, fund = [], {}, {}
    for pair in D.PAIRS:
        rows, h1, series = setups(pair)
        if h1 is None:
            print(f'  {pair}: нет данных 2021')
            continue
        allrows += rows
        data[pair] = h1
        fund[pair] = E.smc_engine.funding_series(*series)
        print(f'  {pair}: сетапов 4ч→1ч в 2021 — {len(rows)}', flush=True)
    frame = pd.DataFrame(allrows).drop_duplicates(['pair', 't', 'dir', 'entry', 'stop'])
    _, _, _, expiry_h, hold_h = M.COMBOS['4ч→1ч']
    print('\n4ч→1ч, 2021, портфель с правилами брокера SMC (исполнение на часовых свечах)')
    for aligned in (True, False):
        for crowd in (False, True):
            f = frame[frame['aligned']] if aligned else frame
            if crowd:
                f = f[f['crowd_ok']]
            for sample, pairs in (('10 пар пула', pool10), ('10 пар вне пула', other10)):
                g = f[f['pair'].isin(pairs)]
                orders = []
                for r in g.itertuples():
                    created = np.datetime64(int(r.t), 'ms')
                    orders.append(E.Order(pair=r.pair, direction='BULLISH' if r.dir > 0 else 'BEARISH', entry=r.entry,
                                          stop=r.stop, targets=[r.target], fractions=[1.0], created=created,
                                          expires=created + np.timedelta64(int(expiry_h * 3600), 's'),
                                          key=(r.pair, int(r.sweep), int(r.dir)), meta={'rr': r.rr}))
                orders.sort(key=lambda o: (o.created, -o.meta['rr']))
                sub = {p: data[p] for p in pairs if p in data}
                for ft in (0.0, 0.0005):
                    E.smc_engine.FILL_THROUGH_PCT = ft
                    E.smc_engine.FUNDING_REAL = {p: fund[p] for p in sub}
                    try:
                        res = E.run_portfolio(orders, sub, risk_pct=1.0, max_positions=5, cooldown_hours=12.0,
                                              breakeven_after_tp1=False, max_hold_hours=hold_h, max_same_direction=3,
                                              occupy_while_pending=True, cooldown_from_placement=True,
                                              cancel_at_target=False)
                    finally:
                        E.smc_engine.FILL_THROUGH_PCT = 0.0
                        E.smc_engine.FUNDING_REAL = None
                    r = np.array([t['pnl'] / t['risk'] for t in res['trades']], dtype=float)
                    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
                    label = (f'{"по старшему ТФ" if aligned else "любое"} | {"толпа" if crowd else "без толпы"} | '
                             f'{sample}{" | насквозь" if ft else ""}')
                    print(f'  {label:52s} {len(r):4d} сд {r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}] '
                          f'итог {r.sum():+7.1f}R', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
