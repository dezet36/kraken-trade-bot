"""
Торговать стратегией, только когда она сама недавно зарабатывала.

ЗАЧЕМ. Роль, которую можно отдать ИИ, не требуя от него угадывать рынок:
включать и выключать стратегию по её собственным недавним результатам
(«кривая доходности как режим»). Здесь проверяется формулой, прежде чем
поручать модели: на портфельных сделках SMC (боевой пул, живые правила) и
Фибо (точная живая симуляция, живые правила брокера) — сделка берётся, только
если сделки этой стратегии, ЗАКРЫТЫЕ за последние N дней до её входа, в сумме
в плюсе. Окна 14 и 30 дней; «нет истории» — сделка берётся.

Честность: фильтр видит только уже закрытые сделки (exit_time < entry_time
текущей), портфель не пересчитывается — это оценка сверху для слотов, которые
освободились бы.

Запуск:
    python research/ai_equity_filter.py
"""
import os
import pickle
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

import logger                                          # noqa: E402
logger.log = lambda *a, **k: None

import ai_doctrine as D                               # noqa: E402
import backtest_smc as bt                             # noqa: E402
from common import ci                                 # noqa: E402
from smc import params as smc_params                  # noqa: E402
from smc_engine import run_portfolio                  # noqa: E402

LIVE = dict(occupy_while_pending=True, cooldown_from_placement=True)
DAY = np.timedelta64(24 * 3600, 's')


def smc_trades(cache):
    bt.CACHE_DIR = os.path.join(HERE, cache)
    data = {p: d for p in bt.DEFAULT_PAIRS if (d := bt.load_pair(p)) is not None}
    orders = []
    for pair, frames in data.items():
        orders += bt.smc_orders(pair, frames)
    base = dict(max_positions=bt.MAX_POSITIONS, cooldown_hours=smc_params.COOLDOWN_HOURS,
                breakeven_after_tp1=smc_params.BREAKEVEN_AFTER_TP1,
                max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS,
                max_same_direction=smc_params.MAX_SAME_DIRECTION, risk_pct=bt.RISK_PCT, **LIVE)
    return run_portfolio(orders, {p: f['5m'] for p, f in data.items()}, **base)['trades']


def fibo_trades(cache):
    path = os.path.join(HERE, 'results', f'fibo_live_orders_{cache}.pkl')
    if not os.path.exists(path):
        return []
    with open(path, 'rb') as fh:
        orders = [o for o in pickle.load(fh) if o.meta.get('cost_share', 0) <= 5.0]
    bt.CACHE_DIR = os.path.join(HERE, cache)
    exec_data = {p: d['5m'] for p in bt.DEFAULT_PAIRS if (d := bt.load_pair(p)) is not None}
    return run_portfolio(orders, exec_data, risk_pct=bt.RISK_PCT, max_positions=99, cooldown_hours=12.0,
                         breakeven_after_tp1=False, max_hold_hours=336.0, max_same_direction=0,
                         cancel_at_target=True, **LIVE)['trades']


def filtered(trades, days):
    trades = sorted(trades, key=lambda t: t['entry_time'])
    exits = np.array([t['exit_time'] for t in trades])
    rs = np.array([t['pnl'] / t['risk'] for t in trades])
    keep = []
    for t in trades:
        lo = t['entry_time'] - days * DAY
        m = (exits < t['entry_time']) & (exits >= lo)
        keep.append(True if not m.any() else rs[m].sum() > 0)
    return np.array([t['pnl'] / t['risk'] for t in trades]), np.array(keep)


def main():
    totals = {}
    for name, builder in (('SMC', smc_trades), ('Фибо', fibo_trades)):
        print(f'\n== {name}', flush=True)
        for period, cache in D.PERIODS.items():
            trades = builder(cache)
            if not trades:
                continue
            line = f'   {period:6s} все {len(trades):4d} сд {np.mean([t["pnl"] / t["risk"] for t in trades]):+.3f}'
            for days in (14, 30):
                rs, keep = filtered(trades, days)
                totals.setdefault((name, days), {'all': [], 'kept': []})
                totals[(name, days)]['all'] += list(rs)
                totals[(name, days)]['kept'] += list(rs[keep])
                line += f' | {days} дн: взято {keep.sum():4d} {rs[keep].mean():+.3f}, пропущено {rs[~keep].mean() if (~keep).any() else float("nan"):+.3f}'
            print(line, flush=True)
    print('\nВСЕ ПЕРИОДЫ:')
    for (name, days), v in totals.items():
        a, k = np.array(v['all']), np.array(v['kept'])
        lo, hi = ci(k)
        print(f'   {name:5s} окно {days} дн: все {a.mean():+.3f} R/сд ({len(a)}), по кривой {k.mean():+.3f} [{lo:+.3f}; {hi:+.3f}] ({len(k)})')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
