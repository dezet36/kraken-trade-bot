"""
ФИБО: перемер геометрии (глубина входа, стоп, цель) на точной симуляции
«как в боте» — docs/Аудит_сделок_2026-10-07.md, раздел 12 (протокол
закоммичен до расчёта, 65f104e).

Геометрия v2 (стоп 0.886, цель 0.25 за B) и глубина входа 0.5 выбирались
бэктестом, где заявка наливалась ценой породившей её свечи. Здесь — заявки
боевого сканера (fibo_live_sim, fibo_other_pairs), импульс восстановлен из
стопа и цели заявки:

    стоп  = B − d·(0.886 + 0.010)·s,   цель = B + d·0.25·s
    → s = (цель − стоп) / (1.146·d),   B = цель − d·0.25·s

(d = +1 лонг, −1 шорт) и проверен по входу заявки. Набор заявок от глубины
не зависит: сканер ставит заявку, пока откат не глубже 38.2% (W11).

    python research/fibo_geometry.py   → results/fibo_geometry.txt
"""
import itertools
import os
import pickle
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fibo_live_sim as FS                            # noqa: E402
import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
from infra import config  # noqa: E402
from accounts import risk_gate  # noqa: E402
from accounts import settings_store  # noqa: E402
import smc_engine                                     # noqa: E402
from common import ci                                 # noqa: E402
from fibo_crowd import funding_at                     # noqa: E402
from fibo_htf_split import BASE                       # noqa: E402
from fibo_other_pairs import OTHER, _path            # noqa: E402

DEPTHS = (0.382, 0.5, 0.618)
STOPS = (0.786, 0.886, 1.0)
TARGETS = (0.0, 0.125, 0.25, 0.382, 0.618)
CURRENT = (0.5, 0.886, 0.25)
CONFIGS = list(itertools.product(DEPTHS, STOPS, TARGETS))
PERIODS = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
           '12m': 'backtest_cache_12m'}
CELLS = [('замер', p) for p in PERIODS] + [('другие', p) for p in PERIODS] + [('2021', 'y21')]
SELECT = [('замер', 'bear'), ('замер', 'mid1')]
VALIDATE = [('замер', 'mid2'), ('замер', '12m')] + [('другие', p) for p in PERIODS] + [('2021', 'y21')]
BUF = config.SL_BUFFER


def load_cell(cell):
    group, period = cell
    if group == '2021':
        cache = 'backtest_cache_y21b'
        with open(os.path.join(HERE, 'results', f'fibo_live_orders_{cache}.pkl'), 'rb') as fh:
            orders = pickle.load(fh)
        lo, hi = pd.Timestamp('2021-01-01'), pd.Timestamp('2022-01-01')
        orders = [o for o in orders if lo <= pd.Timestamp(o.created).tz_localize(None) < hi]
        pairs = sorted({o.pair for o in orders})
    elif group == 'замер':
        cache = PERIODS[period]
        with open(os.path.join(HERE, 'results', f'fibo_live_orders_{cache}.pkl'), 'rb') as fh:
            orders = pickle.load(fh)
        pairs = list(bt.DEFAULT_PAIRS)
    else:
        cache = PERIODS[period]
        pairs = [p for p in OTHER if os.path.exists(_path(cache, p))]
        orders = []
        for p in pairs:
            with open(_path(cache, p), 'rb') as fh:
                orders += pickle.load(fh)
    return cache, orders, pairs


def impulse(o):
    """(B, s, d) из заявки и относительная ошибка восстановленного входа."""
    d = 1.0 if o.direction == 'LONG' else -1.0
    stop, target = o.stop, o.targets[0]
    s = (target - stop) / ((config.SL_LEVEL_R + BUF + config.TP1_LEVEL) * d)
    b = target - d * config.TP1_LEVEL * s
    entry = (b - d * config.ENTRY_RETRACE * s) * (1 + d * FS.OFFSET)
    return b, s, d, abs(entry / o.entry - 1)


def reshape(o, b, s, d, depth, stop_lvl, target_lvl, min_stop):
    raw = b - d * depth * s
    stop = b - d * (stop_lvl + BUF) * s
    if abs(raw - stop) < raw * min_stop:
        return None
    limit = raw * (1 + d * FS.OFFSET)
    share = risk_gate.entry_cost_share(limit, abs(limit - stop), config.ENTRY_COST_ROUND_TRIP) * 100
    if share > FS.COST_LIMIT:
        return None
    return smc_engine.Order(pair=o.pair, direction=o.direction, entry=limit, stop=stop,
                            targets=[b + d * target_lvl * s], fractions=[1.0], created=o.created,
                            expires=o.expires, key=o.key, be_trigger=b if config.BREAKEVEN_AT_B else None,
                            meta={'rr': (target_lvl + depth) / (stop_lvl + BUF - depth)})


def run_cell(cell):
    cache, orders, pairs = load_cell(cell)
    bt.CACHE_DIR = os.path.join(HERE, cache)
    exec_data = {p: d['5m'] for p in pairs if (d := bt.load_pair(p)) is not None}
    fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in pairs}
    min_stop = settings_store.min_stop_pct('FIBO')
    geo, errs, f3 = {}, [], {}
    for o in orders:
        b, s, d, err = impulse(o)
        geo[id(o)] = (b, s, d)
        errs.append(err)
        t = int(pd.Timestamp(o.created).tz_localize('UTC').value // 10 ** 6) \
            if pd.Timestamp(o.created).tzinfo is None else int(pd.Timestamp(o.created).value // 10 ** 6)
        f3[id(o)] = o.direction == 'SHORT' and funding_at(fund.get(o.pair), t) * -1.0 <= 0
    errs = np.array(errs)
    out = {'_check': (len(orders), float(np.median(errs)), float(errs.max()))}
    for cfg in CONFIGS:
        built = [(reshape(o, *geo[id(o)], *cfg, min_stop), f3[id(o)]) for o in orders]
        pools = {'обе': [x for x, _ in built if x is not None],
                 'F3': [x for x, ok in built if x is not None and ok]}
        for pool, lst in pools.items():
            for through in (0.0, 0.0005):
                smc_engine.FILL_THROUGH_PCT = through
                try:
                    res = smc_engine.run_portfolio(lst, exec_data, **BASE)
                finally:
                    smc_engine.FILL_THROUGH_PCT = 0.0
                out[(cfg, pool, through)] = np.array([t['pnl'] / t['risk'] for t in res['trades']])
    print(f'   {cell}: заявок {len(orders)}, ошибка восстановления входа медиана '
          f'{out["_check"][1]:.2e}, макс {out["_check"][2]:.2e}', flush=True)
    return cell, out


def mean(a):
    return float(a.mean()) if len(a) else float('nan')


def main():
    with Pool(min(len(CELLS), os.cpu_count() or 2)) as pool:
        res = dict(pool.imap_unordered(run_cell, CELLS))

    def pooled(cells, cfg, pool='обе', through=0.0):
        return np.concatenate([res[c][(cfg, pool, through)] for c in cells])

    print('\nСЕТКА: R на сделку (касание), обе стороны. Отбор = замер bear+mid1; проверка = 7 ячеек')
    print(f'{"глубина/стоп/цель":18s} {"отбор":>16s} {"проверка":>16s} {"проверка насквозь":>18s}  '
          + ' '.join(f'{g[:3]}-{p}' for g, p in CELLS))
    rows = []
    for cfg in CONFIGS:
        sel, val, valt = pooled(SELECT, cfg), pooled(VALIDATE, cfg), pooled(VALIDATE, cfg, through=0.0005)
        rows.append((cfg, sel, val, valt))
        mark = ' ← сейчас' if cfg == CURRENT else ''
        print(f'{cfg[0]:.3f}/{cfg[1]:.3f}/{cfg[2]:.3f}  {mean(sel):+.3f} ({len(sel):5d})  {mean(val):+.3f} ({len(val):5d})'
              f'  {mean(valt):+.3f}            '
              + ' '.join(f'{mean(res[c][(cfg, "обе", 0.0)]):+.3f}' for c in CELLS) + mark)

    eligible = [r for r in rows if len(r[1]) >= 300]
    best = max(eligible, key=lambda r: mean(r[1]))
    cfg = best[0]
    print(f'\nВЫБРАНО НА ОТБОРЕ: глубина {cfg[0]}, стоп {cfg[1]}, цель {cfg[2]} — отбор {mean(best[1]):+.3f} '
          f'против текущей {mean(pooled(SELECT, CURRENT)):+.3f}')
    print('ПРИЁМКА: R на сделку выше текущей в каждой проверочной ячейке')
    ok = True
    for c in VALIDATE:
        a, b = mean(res[c][(cfg, 'обе', 0.0)]), mean(res[c][(CURRENT, 'обе', 0.0)])
        ok &= a > b
        print(f'   {c[0]:6s} {c[1]:4s}: {a:+.3f} против {b:+.3f} — {"выше" if a > b else "НЕ выше"}')
    valt = pooled(VALIDATE, cfg, through=0.0005)
    lo, hi = ci(valt)
    print(f'   проверка насквозь: {mean(valt):+.3f} [{lo:+.3f}; {hi:+.3f}] — {"> 0" if mean(valt) > 0 else "≤ 0"}')
    print(f'   ИТОГ: {"ПРИНЯТА" if ok and mean(valt) > 0 else "не принята"}')

    print('\nДля понимания — пул F3 (шорты против толпы, как в боте): выбранная против текущей')
    for c in CELLS:
        print(f'   {c[0]:6s} {c[1]:4s}: {mean(res[c][(cfg, "F3", 0.0)]):+.3f} ({len(res[c][(cfg, "F3", 0.0)])}) '
              f'против {mean(res[c][(CURRENT, "F3", 0.0)]):+.3f} ({len(res[c][(CURRENT, "F3", 0.0)])})')
    with open(os.path.join(HERE, 'results', 'fibo_geometry.pkl'), 'wb') as fh:
        pickle.dump({k: {kk: v for kk, v in d.items()} for k, d in res.items()}, fh)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
