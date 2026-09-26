"""
ИИ в режиме «правила» (Live_Bot/llm_rules) на пяти периодах — ровно то, что
будет торговать бот.

Сетапы — ядро smc.signal с ПРАВИЛАМИ ИИ (evaluate(decision=llm_rules.DECISION)),
пул — llm_rules.POOL, события — решения ФРС ±llm_rules.EVENT_WINDOW_H без
входов, портфель — живые правила брокера со значениями ИИ из его правил
(кулдаун, направленный кэп, срок заявки, удержание, без безубытка, заявка
занимает пару, пауза с постановки, без снятия у цели).

Проверка честности: без календаря событий результат обязан совпасть с
портфелем SMC на тех же парах сделка в сделку — правила ИИ сейчас копия.

Запуск:
    python research/ai_rules_backtest.py
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

import ai_doctrine as D                               # noqa: E402
import backtest_smc as bt                             # noqa: E402
import llm_rules                                      # noqa: E402
from ai_filter_study import FOMC                      # noqa: E402
from common import ci                                 # noqa: E402
from smc import signal as smc_signal                  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402

R = llm_rules.DECISION
EVENTS_MS = np.array(sorted(int(pd.Timestamp(d + ' 18:00', tz='UTC').value // 10 ** 6) for d in FOMC))


def rules_orders(pair, data, decision):
    ctx = smc_signal.build_context({'bias': data['1d'], 'htf': data['4h'], 'poi': data['1h']}, pair=pair)
    df = data['1h']
    orders, seen = [], set()
    expiry = np.timedelta64(int(R.PENDING_ORDER_MAX_HOURS * 3600), 's')
    for i in range(60, len(df)):
        setup, _why = ctx.evaluate(i, balance=bt.INITIAL_BALANCE, decision=decision)
        if setup is None:
            continue
        poi = setup['poi']
        key = (pair, poi['type'], poi['index'], setup['direction'])
        if key in seen:
            continue
        seen.add(key)
        created = np.datetime64(pd.Timestamp(setup['time']).tz_convert('UTC').tz_localize(None)) + bt._SIGNAL_BAR
        trade = setup['params']
        orders.append(Order(pair=pair, direction=setup['direction'], entry=trade['entry'],
                            stop=trade['stop_loss'], targets=trade['targets'], fractions=trade['fractions'],
                            created=created, expires=created + expiry, key=key,
                            meta={'confluence': setup['confluence']}))
    return orders


def near_event(order, window_h):
    t = int(pd.Timestamp(order.created).value // 10 ** 6)
    k = int(np.searchsorted(EVENTS_MS, t))
    gaps = [abs(t - EVENTS_MS[j]) for j in (k - 1, k) if 0 <= j < len(EVENTS_MS)]
    return bool(gaps) and min(gaps) <= window_h * 3_600_000


def main():
    live = dict(max_positions=bt.MAX_POSITIONS, cooldown_hours=R.COOLDOWN_HOURS,
                breakeven_after_tp1=R.BREAKEVEN_AFTER_TP1, max_hold_hours=R.MAX_POSITION_HOLD_HOURS,
                max_same_direction=R.MAX_SAME_DIRECTION, risk_pct=bt.RISK_PCT,
                occupy_while_pending=True, cooldown_from_placement=True,
                cancel_at_target=R.CANCEL_PENDING_AT_TARGET)
    totals = {}
    for period, cache in D.PERIODS.items():
        bt.CACHE_DIR = os.path.join(HERE, cache)
        data = {p: d for p in llm_rules.POOL if (d := bt.load_pair(p)) is not None}
        orders = []
        for pair, frames in data.items():
            orders += rules_orders(pair, frames, R)
        smc_same = []
        for pair, frames in data.items():
            smc_same += bt.smc_orders(pair, frames)
        exec_data = {p: f['5m'] for p, f in data.items()}
        variants = [
            ('SMC на тех же парах (сверка)', smc_same),
            ('ИИ: правила, без календаря', orders),
            ('ИИ: правила + ФРС ±24 ч', [o for o in orders if not near_event(o, llm_rules.EVENT_WINDOW_H)]),
        ]
        print(f'\n== {period}: пар {len(data)}, заявок ИИ {len(orders)}', flush=True)
        for name, pool in variants:
            out = run_portfolio(pool, exec_data, **live)
            rs = [t['pnl'] / t['risk'] for t in out['trades']]
            s = compute_stats(out)
            totals.setdefault(name, []).extend(rs)
            print(f'   {name:30s} {len(rs):4d} сд  {sum(rs):+7.1f}R  {np.mean(rs) if rs else 0:+.3f} R/сд  '
                  f'PF {s.get("profit_factor", 0):.2f}  просадка {s.get("max_dd_pct", 0):5.1f}%  '
                  f'доходность {s.get("total_return_pct", s.get("return_pct", 0)):+.1f}%', flush=True)
    print('\nВСЕ ПЯТЬ ПЕРИОДОВ (12m и fresh частично перекрываются):')
    for name, rs in totals.items():
        r = np.array(rs)
        lo, hi = ci(r)
        print(f'   {name:30s} {len(r):4d} сд  {r.sum():+7.1f}R  {r.mean():+.3f} R/сд [{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
