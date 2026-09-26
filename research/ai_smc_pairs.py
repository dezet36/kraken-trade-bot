"""
Правила SMC на других парах: боевой пул против остальных десяти.

ЗАЧЕМ. Если ИИ-счёт торгует сетапы SMC, на тех же парах он повторит SMC
сделка в сделку. Своя ниша — те же правила решений на парах, которых нет в
боевом пуле SMC (ARB, SUI, UNI, ZEC, AAVE, DOT, XLM, SHIB1000, NEAR, COTI).
Пул SMC сокращён до 10 «валидированных» пар 04.07.2026 по бэктесту — отбор
по исходу, и остальные пары могут быть хуже. Здесь это меряется, портфель с
живыми правилами SMC, пять периодов.

Запуск:
    python research/ai_smc_pairs.py
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

import ai_doctrine as D                               # noqa: E402
import backtest_smc as bt                             # noqa: E402
from common import ci                                 # noqa: E402
from smc import params as smc_params                  # noqa: E402
from smc_engine import compute_stats, run_portfolio   # noqa: E402

LIVE = dict(occupy_while_pending=True, cooldown_from_placement=True)
OTHER = [p for p in D.PAIRS if p not in bt.DEFAULT_PAIRS]


def main():
    base = dict(max_positions=bt.MAX_POSITIONS, cooldown_hours=smc_params.COOLDOWN_HOURS,
                breakeven_after_tp1=smc_params.BREAKEVEN_AFTER_TP1,
                max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS,
                max_same_direction=smc_params.MAX_SAME_DIRECTION, risk_pct=bt.RISK_PCT, **LIVE)
    totals = {}
    for period, cache in D.PERIODS.items():
        bt.CACHE_DIR = os.path.join(HERE, cache)
        print(f'\n== {period}', flush=True)
        for name, pairs in (('боевой пул SMC', bt.DEFAULT_PAIRS), ('другие 10 пар', OTHER)):
            data = {p: d for p in pairs if (d := bt.load_pair(p)) is not None}
            if not data:
                print(f'   {name}: пар в кэше нет')
                continue
            orders = []
            for pair, frames in data.items():
                orders += bt.smc_orders(pair, frames)
            out = run_portfolio(orders, {p: f['5m'] for p, f in data.items()}, **base)
            rs = [t['pnl'] / t['risk'] for t in out['trades']]
            s = compute_stats(out)
            totals.setdefault(name, []).extend(rs)
            print(f'   {name:16s} пар {len(data):2d}  заявок {len(orders):4d}  сделок {len(rs):4d}  {sum(rs):+7.1f}R  '
                  f'{np.mean(rs) if rs else 0:+.3f} R/сд  PF {s.get("profit_factor", 0):.2f}  '
                  f'просадка {s.get("max_dd_pct", 0):5.1f}%', flush=True)
    print('\nВСЕ ПЯТЬ ПЕРИОДОВ (12m и fresh частично перекрываются):')
    for name, rs in totals.items():
        r = np.array(rs)
        lo, hi = ci(r)
        print(f'   {name:16s} {len(r):4d} сд  {r.sum():+7.1f}R  {r.mean():+.3f} R/сд [{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
