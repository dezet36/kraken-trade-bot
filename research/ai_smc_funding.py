"""
SMC с правилом фандинга: не входить туда, куда толпа платит.

ОТКУДА ГИПОТЕЗА. research/ai_filter_study.py: у SMC среднее трёх последних
выплат фандинга в сторону сделки — единственный признак, державший знак во
всех четырёх периодах: верхняя пятая часть хуже нижней на 0.46R
[−0.75; −0.17]. Тот же знак у Фибо и у доктрины ИИ. Это то, что ИИ видит в
разметке («Фандинг … плюс — платят лонги») и что ни одна стратегия не читает.

ПРАВИЛО — БЕЗ ПОДБОРА. Порог — базовая ставка биржи, а не квантиль:
    лонг пропускается, если среднее трёх выплат выше 0.01% (лонги доплачивают
    сверх базы — позиция переполнена);
    шорт пропускается, если среднее трёх выплат ниже нуля (платят шорты).

Портфель — живые правила SMC (research/broker_rules.py «живые правила, заявка
ждёт у цели»), 10 пар боевого пула, пять периодов.

Запуск:
    python research/ai_smc_funding.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

import logger                                          # noqa: E402
logger.log = lambda *a, **k: None

import pandas as pd                                    # noqa: E402

import ai_doctrine as D                               # noqa: E402
import backtest_smc as bt                             # noqa: E402
from common import ci                                 # noqa: E402
from smc import params as smc_params                  # noqa: E402
from smc_engine import compute_stats, run_portfolio   # noqa: E402

LIVE = dict(occupy_while_pending=True, cooldown_from_placement=True)


def crowded(order, funding):
    if funding is None:
        return False
    ts, rate = funding
    t_ms = int(pd.Timestamp(order.created).value // 10 ** 6)
    k = int(np.searchsorted(ts, t_ms, side='right')) - 1
    if k < 2:
        return False
    avg = rate[k - 2:k + 1].mean()
    long_ = order.direction in ('BULLISH', 'LONG')
    return avg > 0.0001 if long_ else avg < 0


def main():
    base = dict(max_positions=bt.MAX_POSITIONS, cooldown_hours=smc_params.COOLDOWN_HOURS,
                breakeven_after_tp1=smc_params.BREAKEVEN_AFTER_TP1,
                max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS,
                max_same_direction=smc_params.MAX_SAME_DIRECTION, risk_pct=bt.RISK_PCT, **LIVE)
    totals = {'SMC как сейчас': [], 'SMC без входов по толпе': [], 'только входы по толпе': []}
    for period, cache in D.PERIODS.items():
        bt.CACHE_DIR = os.path.join(HERE, cache)
        data = {p: d for p in bt.DEFAULT_PAIRS if (d := bt.load_pair(p)) is not None}
        orders, flags = [], []
        for pair, frames in data.items():
            found = bt.smc_orders(pair, frames)
            fund = D.load_funding(cache, pair)
            orders += found
            flags += [crowded(o, fund) for o in found]
        exec_data = {p: f['5m'] for p, f in data.items()}
        crowd = sum(flags)
        print(f'\n== {period}: заявок {len(orders)}, по толпе {crowd} ({crowd / max(1, len(orders)) * 100:.0f}%)', flush=True)
        pools = {
            'SMC как сейчас': orders,
            'SMC без входов по толпе': [o for o, f in zip(orders, flags) if not f],
            'только входы по толпе': [o for o, f in zip(orders, flags) if f],
        }
        for name, pool in pools.items():
            out = run_portfolio(pool, exec_data, **base)
            trades = out['trades']
            rs = [t['pnl'] / t['risk'] for t in trades]
            s = compute_stats(out)
            totals[name] += rs
            print(f'   {name:26s} {len(rs):4d} сд  {sum(rs):+7.1f}R  {np.mean(rs) if rs else 0:+.3f} R/сд  '
                  f'PF {s.get("profit_factor", 0):.3f}  просадка {s.get("max_dd_pct", 0):5.1f}%', flush=True)
    print('\nВСЕ ПЯТЬ ПЕРИОДОВ (12m и fresh частично перекрываются):')
    for name, rs in totals.items():
        r = np.array(rs)
        lo, hi = ci(r)
        print(f'   {name:26s} {len(r):4d} сд  {r.sum():+7.1f}R  {r.mean():+.3f} R/сд [{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
