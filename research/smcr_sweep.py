"""
Разбор SMC, раздел 8Б (docs/SMC_разбор_аналитика_2026-10-01.md): вход по
снятию ликвидности по тренду — новый тип сетапа на готовом общем слое
(пулы, снятия, направление дня и 4 ч из smc/), без правок бота.

Сетап: направление дня и 4 ч совпадают; на часе закрылся возврат после
снятия пула ПРОТИВ этого направления (для лонга — снят минимум). Стоп — за
экстремумом снятия (все свечи от прокола до возврата) +0.15%.

    python research/smcr_sweep.py gen     # → results/smcr/sweep_rows_<период>.pkl (20 пар, 5 периодов)
    python research/smcr_sweep.py eval    # → печать (results/smcr/eval_sweep.txt)
"""
import itertools
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

OUT = os.path.join(HERE, 'results', 'smcr')
CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m', 'fresh': 'backtest_cache_fresh20'}
PERIODS = ('bear', 'mid1', 'mid2', '12m', 'fresh')
H_MS = 3_600_000
BUFFER = 0.0015
MIN_STOP = 0.008


def _job(args):
    period, pair = args
    from infra import logger
    logger.log = lambda *a, **k: None
    import backtest_smc as bt
    from strategies.smc import liquidity, signal as smc_signal
    bt.CACHE_DIR = os.path.join(HERE, CACHES[period])
    df_1h = bt.load_cached(pair, '1h')
    if df_1h is None or not os.path.exists(os.path.join(bt.CACHE_DIR, f'{pair}_5m.pkl')):
        return period, pair, [], 0.0
    started = time.time()
    df_4h = bt.load_cached(pair, '4h')
    if df_4h is None:
        df_4h = bt.resample(df_1h, '4h')
    ctx = smc_signal.build_context({'bias': bt.resample(df_1h, '1D'), 'htf': df_4h, 'poi': df_1h}, pair=pair)
    high = df_1h['high'].to_numpy(float)
    low = df_1h['low'].to_numpy(float)
    close = df_1h['close'].to_numpy(float)
    stamps = df_1h['timestamp']
    best = {}
    for s in ctx.sweeps:
        i = int(s['reclaimed_at'])
        if i < 60 or i >= len(df_1h):
            continue
        bias = ctx.bias_at(stamps.iloc[i])
        if bias != s['direction']:
            continue                                    # снятие ПРОТИВ отката по тренду: лонг — снят минимум
        side = 1 if bias == 'BULLISH' else -1
        a = int(s['index'])
        ext = float(low[a:i + 1].min()) if side > 0 else float(high[a:i + 1].max())
        key = (i, side)
        cur = best.get(key)
        if cur is None or s['weight'] > cur['weight']:
            # цели: нетронутые пулы с той стороны (как у ядра SMC)
            opp = liquidity.untapped_pools(ctx.pools, ctx.sweeps, i,
                                           side=liquidity.BSL if side > 0 else liquidity.SSL)
            prices = sorted({float(p['price']) for p in opp
                             if (p['price'] > close[i] if side > 0 else p['price'] < close[i])},
                            reverse=side < 0)[:8]
            best[key] = {'period': period, 'pair': pair, 'i': i, 'dir': side,
                         't': int(pd.Timestamp(stamps.iloc[i]).value // 10 ** 6) + H_MS,
                         'level': float(s['level']), 'extreme': ext, 'weight': float(s['weight']),
                         'source': s['source'], 'close': float(close[i]), 'pools': prices,
                         'n_pools': 1 if cur is None else cur.get('n_pools', 1) + 1}
        else:
            cur['n_pools'] = cur.get('n_pools', 1) + 1
    return period, pair, list(best.values()), time.time() - started


def generate():
    import ai_doctrine as D
    os.makedirs(OUT, exist_ok=True)
    jobs = [(p, pair) for p in PERIODS for pair in D.PAIRS
            if not os.path.exists(os.path.join(OUT, f'sweep_rows_{p}.pkl'))]
    acc = {}
    with Pool(4) as pool:
        for period, pair, rows, sec in pool.imap_unordered(_job, jobs):
            acc.setdefault(period, []).extend(rows)
            print(f'  {period:5s} {pair:13s} сетапов {len(rows):5d}  {sec:5.1f} с', flush=True)
    for period, rows in acc.items():
        path = os.path.join(OUT, f'sweep_rows_{period}.pkl')
        pd.DataFrame(rows).to_pickle(path + '.tmp')
        os.replace(path + '.tmp', path)
        print(f'{period}: {len(rows)} → {path}', flush=True)


# ── Оценка ───────────────────────────────────────────────────────────────────
def orders(period, rows, pairs, entry, target, crowd, prep):
    import smcr_live as L
    E = L.E
    out = []
    for r in rows.itertuples():
        if r.pair not in pairs:
            continue
        arr = prep.get(r.pair)
        if arr is None:
            continue
        created = np.datetime64(int(r.t), 'ms')
        k = int(np.searchsorted(arr['ts'], created, side='right'))
        if k >= len(arr['ts']):
            continue
        side = r.dir
        stop = r.extreme * (1 - BUFFER) if side > 0 else r.extreme * (1 + BUFFER)
        if entry == 'mkt':
            e = float(arr['open'][k])
            etype, expiry_h = 'stop', 1.0
        else:
            e = float(r.level)
            etype, expiry_h = 'limit', 12.0
        risk = (e - stop) * side
        if risk <= 0 or risk / e < MIN_STOP:
            continue                                        # стоп теснее 0.8% или за входом
        if crowd:
            x = crowd_bp(period, r.pair, int(r.t), side)
            if np.isfinite(x) and x > -1.0:
                continue                                    # фильтр толпы бота
        if target in (2.0, 3.0):
            tg = e + side * target * risk
        else:
            far = [p for p in r.pools if (p - e) * side >= 2.0 * risk]
            tg = far[0] if far else e + side * 6.0 * risk
            if (tg - e) * side > 6.0 * risk:
                tg = e + side * 6.0 * risk
        out.append(E.Order(pair=r.pair, direction='BULLISH' if side > 0 else 'BEARISH', entry=e, stop=float(stop),
                           targets=[float(tg)], fractions=[1.0], created=created,
                           expires=created + np.timedelta64(int(expiry_h * 3600), 's'),
                           key=(r.pair, int(r.i), side), entry_type=etype,
                           meta={'t': int(r.t), 'period': period}))
    return out


_fund = {}


def crowd_bp(period, pair, t, side):
    import smcr_live as L
    k = (period, pair)
    if k not in _fund:
        _fund[k] = L.E.S.load_series(L.D.PERIODS[period], 'funding', pair, 'funding_rate')
    got = _fund[k]
    if got is None:
        return np.nan
    ts, vals = got
    j = int(np.searchsorted(ts, t, side='right')) - 1
    return vals[j] * 1e4 * side if j >= 0 else np.nan


def evaluate():
    import smcr_live as L
    from common import ci
    E = L.E
    pool10 = L.POOL10
    other10 = tuple(p for p in L.D.PAIRS if p not in pool10)
    end12 = E.END_12M_MS
    variants = list(itertools.product(('w07', 'all'), ('mkt', 'retest'), (2.0, 3.0, 'pool'), (False, True)))
    store = {}                                  # (вариант, выборка, ft, период) -> [R]
    for period in PERIODS:
        rows = pd.read_pickle(os.path.join(OUT, f'sweep_rows_{period}.pkl'))
        for sample, pairs in (('pool', pool10), ('other', other10)):
            data = E.exec_data(period, pairs)
            prep = {p: E._prepare(df) for p, df in data.items()}
            fund = E.funding_data(period, pairs)
            for v in variants:
                liq, entry, target, crowd = v
                f = rows[rows['weight'] >= 0.7] if liq == 'w07' else rows
                ords = orders(period, f, pairs, entry, target, crowd, prep)
                for ft in (0.0, 0.0005):
                    E.smc_engine.FILL_THROUGH_PCT = ft
                    E.smc_engine.FUNDING_REAL = fund
                    try:
                        out = E.run_portfolio(ords, data, risk_pct=1.0, max_positions=5, cooldown_hours=12.0,
                                              breakeven_after_tp1=False, max_hold_hours=336.0, max_same_direction=3,
                                              occupy_while_pending=True, cooldown_from_placement=True,
                                              cancel_at_target=False)
                    finally:
                        E.smc_engine.FILL_THROUGH_PCT = 0.0
                        E.smc_engine.FUNDING_REAL = None
                    rs = [(t['meta']['t'], t['pnl'] / t['risk']) for t in out['trades']]
                    if period == 'fresh':
                        rs = [x for x in rs if x[0] > end12]          # без двойного счёта
                    store[(v, sample, ft, period)] = [x[1] for x in rs]
            print(f'  {period} {sample}: готово', flush=True)
        E._exec.pop(period, None)
    results = []
    print()
    for v in variants:
        liq, entry, target, crowd = v
        label = (f'{"пулы ≥0.7" if liq == "w07" else "все пулы"} | {"рынок" if entry == "mkt" else "ретест 12 ч"} | '
                 f'{(str(target) + "R") if target != "pool" else "пул"} | {"толпа" if crowd else "без толпы"}')
        per = {p: store[(v, 'pool', 0.0, p)] for p in PERIODS}
        r = np.array([x for p in PERIODS for x in per[p]])
        early = sum(sum(per[p]) for p in ('bear', 'mid1'))
        late = sum(sum(per[p]) for p in ('mid2', '12m', 'fresh'))
        late_ft = sum(sum(store[(v, 'pool', 0.0005, p)]) for p in ('mid2', '12m', 'fresh'))
        r_o = np.array([x for p in PERIODS for x in store[(v, 'other', 0.0, p)]])
        lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
        results.append((early, label, late, late_ft, r_o.sum()))
        print(f'  {label:44s} {len(r):5d} сд ({len(r) / 4.7:4.0f}/год) {r.mean() if len(r) else 0:+.3f} '
              f'[{lo:+.3f}; {hi:+.3f}] итог {r.sum():+7.1f}R | отбор {early:+6.1f} приёмка {late:+6.1f} '
              f'(насквозь {late_ft:+6.1f}) | вне пула {len(r_o)} сд {r_o.sum():+6.1f}R | '
              + ' '.join(f'{p} {sum(per[p]):+.1f}' for p in PERIODS), flush=True)
    best = max(results, key=lambda x: x[0])
    print()
    print(f'Лучший по отбору: {best[1]} — отбор {best[0]:+.1f}R, приёмка {best[2]:+.1f}R (насквозь {best[3]:+.1f}), '
          f'вне пула {best[4]:+.1f}R → {"держится" if best[2] > 0 and best[3] > 0 and best[4] > 0 else "НЕ держится"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['gen']:
        generate()
    elif sys.argv[1:2] == ['eval']:
        evaluate()
