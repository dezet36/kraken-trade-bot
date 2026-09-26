"""
Фильтр поверх Фибо: портфель с живыми правилами брокера, вне выборки.

ЗАЧЕМ. research/ai_filter_study.py показал, что по признакам момента
постановки (фандинг, геометрия, режим) лучшую половину сетапов Фибо можно
отобрать на периоде, которого фильтр не видел: +0.04…+0.06R на сделку против
−0.026R у всех, и так в каждом из четырёх периодов. Но там каждая заявка
исполнена отдельно. Живой бот держит пару занятой заявкой, ставит паузу с
постановки, снимает заявку у цели — и отбор меняет, КАКИЕ заявки вообще
встанут. Проверка, которая что-то значит, — тот же портфель, что у бота.

Фильтр — гребневая регрессия (ai_filter_study.fit_ridge) на всех признаках,
обучена на ТРЁХ периодах, решает на четвёртом. Порог — квантиль прогноза на
обучении, не на проверке: в живой работе распределения будущих прогнозов нет.
Заявки — точная симуляция живой Фибо (results/fibo_live_orders_<кэш>.pkl),
правила брокера — как в research/fibo_live_sim.report.

Запуск:
    python research/ai_fibo_filter.py
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_filter_study as F                           # noqa: E402
import backtest_smc as bt                             # noqa: E402
from smc_engine import compute_stats, run_portfolio   # noqa: E402

FOLDS = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1',
         'mid2': 'backtest_cache_mid2', '12m': 'backtest_cache_12m'}
COST_LIMIT = 5.0
LIVE = dict(risk_pct=bt.RISK_PCT, max_positions=99, cooldown_hours=12.0, breakeven_after_tp1=False,
            max_hold_hours=336.0, max_same_direction=0, occupy_while_pending=True,
            cooldown_from_placement=True, cancel_at_target=True)


def load_rows():
    frames = [pd.read_pickle(os.path.join(HERE, 'results', f'ai_setups_fibo_{p}.pkl')) for p in FOLDS]
    rows = pd.concat(frames, ignore_index=True)
    rows['fold'] = rows['period']
    return rows


def summary(trades, stats):
    total = sum(t['pnl'] / t['risk'] for t in trades)
    return (f"{len(trades):5d} сд  {total:+8.1f}R  {total / max(1, len(trades)):+.3f} R/сд  "
            f"PF {stats.get('profit_factor', 0):.3f}  просадка {stats.get('max_dd_pct', 0):5.1f}%")


def main():
    rows = load_rows()
    cols = [c for c in F.NUMERIC if c in rows and rows[c].notna().mean() > 0.5 and c != 'fomc_hours']
    print('признаки фильтра:', ', '.join(cols))
    totals = {}
    for fold, cache in FOLDS.items():
        train = rows[(rows.fold != fold) & rows.filled & rows.r.notna()]
        test = rows[rows.fold == fold].copy()
        xt, stats = F.design(train, cols)
        w = F.fit_ridge(xt, np.clip(train.r.to_numpy(), -2, 6))
        train_pred = xt @ w
        test['pred'] = F.design(test, cols, stats)[0] @ w
        by_key = {(p, int(t), s): v for p, t, s, v in zip(test.pair, test.t, test.side, test.pred)}

        with open(os.path.join(HERE, 'results', f'fibo_live_orders_{cache}.pkl'), 'rb') as fh:
            orders = [o for o in pickle.load(fh) if o.meta.get('cost_share', 0) <= COST_LIMIT]
        # Прогноз сетапа — по первой постановке его ключа (признаки у набора —
        # на первую постановку); повторные постановки того же ключа — тот же сетап.
        first = {}
        for o in sorted(orders, key=lambda o: o.created):
            if o.key not in first:
                first[o.key] = by_key.get((o.pair, int(pd.Timestamp(o.created).value // 10 ** 6), o.direction))
        bt.CACHE_DIR = os.path.join(HERE, cache)
        exec_data = {p: d['5m'] for p in bt.DEFAULT_PAIRS if (d := bt.load_pair(p)) is not None}
        print(f'\n== {fold}: заявок {len(orders)}, сетапов {len(first)}, с прогнозом '
              f'{sum(v is not None for v in first.values())}')
        for label, q in (('все заявки (живая Фибо)', None), ('фильтр: верхние 50%', 0.5), ('фильтр: верхние 30%', 0.7)):
            if q is None:
                pool = orders
            else:
                cut = float(np.quantile(train_pred, q))
                pool = [o for o in orders if first.get(o.key) is not None and first[o.key] >= cut]
            out = run_portfolio(pool, exec_data, **LIVE)
            s = compute_stats(out)
            print(f'   {label:28s} {summary(out["trades"], s)}', flush=True)
            totals.setdefault(label, []).extend(t['pnl'] / t['risk'] for t in out['trades'])
    print('\nВСЕ ЧЕТЫРЕ ПЕРИОДА:')
    for label, rs in totals.items():
        r = np.array(rs)
        lo, hi = F.ci(r)
        print(f'   {label:28s} {len(r):5d} сд  {r.sum():+8.1f}R  {r.mean():+.3f} R/сд [{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
