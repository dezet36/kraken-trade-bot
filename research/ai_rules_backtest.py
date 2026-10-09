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

ПРЕДЕЛ ИЗДЕРЖЕК (исправление 27.09.2026). Первая версия не знала, что брокер
отказывает заявке, если комиссии круга съедают больше MAX_ENTRY_COST_SHARE_PCT
риска (risk_gate.cost_too_high; у ИИ 10%, смещения лимита нет — значит, стоп
теснее 0.75% не ставится). Правила разрешают стоп от 0.5%, и почти весь плюс
первой версии (+155.7R) давали именно такие сделки. Строки «как брокер» —
то, что бот сделал бы на самом деле. Разбивка по ширине стопа и налив
«насквозь» (smc_engine.FILL_THROUGH_PCT) показывают, откуда берётся плюс и
держится ли он на касаниях.

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
import config                                         # noqa: E402
import llm_rules                                      # noqa: E402
import risk_gate                                      # noqa: E402
import smc_engine                                     # noqa: E402
from ai_filter_study import FOMC                      # noqa: E402
from common import ci                                 # noqa: E402
from strategies.smc import signal as smc_signal                  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402

R = llm_rules.DECISION
EVENTS_MS = np.array(sorted(int(pd.Timestamp(d + ' 18:00', tz='UTC').value // 10 ** 6) for d in FOMC))
STOP_BUCKETS = ((0.0, 0.75), (0.75, 1.0), (1.0, 1.5), (1.5, 99.0))
FILL_THROUGH = 0.0005                                 # налив «насквозь» на 0.05% цены


def rules_orders(pair, data, decision):
    ctx = smc_signal.build_context({'bias': data['1d'], 'htf': data['4h'], 'poi': data['1h']}, pair=pair)
    df = data['1h']
    orders, seen = [], set()
    expiry = np.timedelta64(int(R.PENDING_ORDER_MAX_HOURS * 3600), 's')
    for i in range(60, len(df)):
        setup, _why = ctx.evaluate(i, decision=decision)
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
                            meta={'confluence': setup['confluence'],
                                  'stop_pct': abs(trade['entry'] - trade['stop_loss']) / trade['entry'] * 100}))
    return orders


def near_event(order, window_h):
    t = int(pd.Timestamp(order.created).value // 10 ** 6)
    k = int(np.searchsorted(EVENTS_MS, t))
    gaps = [abs(t - EVENTS_MS[j]) for j in (k - 1, k) if 0 <= j < len(EVENTS_MS)]
    return bool(gaps) and min(gaps) <= window_h * 3_600_000


def broker_takes(order):
    """Пропустит ли заявку боевой брокер по пределу издержек (смещение лимита у ИИ — 0)."""
    pricey, _share, _why = risk_gate.cost_too_high(order.entry, abs(order.entry - order.stop),
                                                   config.ENTRY_COST_ROUND_TRIP, R.MAX_ENTRY_COST_SHARE_PCT)
    return not pricey


def no_event(orders):
    return [o for o in orders if not near_event(o, llm_rules.EVENT_WINDOW_H)]


def main():
    live = dict(max_positions=bt.MAX_POSITIONS, cooldown_hours=R.COOLDOWN_HOURS,
                breakeven_after_tp1=R.BREAKEVEN_AFTER_TP1, max_hold_hours=R.MAX_POSITION_HOLD_HOURS,
                max_same_direction=R.MAX_SAME_DIRECTION, risk_pct=bt.RISK_PCT,
                occupy_while_pending=True, cooldown_from_placement=True,
                cancel_at_target=R.CANCEL_PENDING_AT_TARGET)
    print(f'предел издержек ИИ {R.MAX_ENTRY_COST_SHARE_PCT}% риска, круг {config.ENTRY_COST_ROUND_TRIP * 100:.3f}% '
          f'→ стоп не теснее {config.ENTRY_COST_ROUND_TRIP * 100 / R.MAX_ENTRY_COST_SHARE_PCT * 100:.2f}%; '
          f'минимальный стоп правил {R.MIN_SL_PCT * 100:.2f}%')
    totals, buckets = {}, {b: [] for b in STOP_BUCKETS}
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
        broker = [o for o in orders if broker_takes(o)]
        variants = [
            ('SMC на тех же парах (сверка)', smc_same, 0.0),
            ('ИИ: правила, без календаря', orders, 0.0),
            ('ИИ: правила + ФРС ±24 ч', no_event(orders), 0.0),
            ('как брокер: издержки ≤10%', broker, 0.0),
            ('как брокер + ФРС ±24 ч  ← бот', no_event(broker), 0.0),
            ('стоп ≥ 0.8% (как живая SMC)', [o for o in orders if o.meta['stop_pct'] >= 0.8], 0.0),
            ('только стоп < 0.75%', [o for o in orders if o.meta['stop_pct'] < 0.75], 0.0),
            ('правила + ФРС, налив насквозь', no_event(orders), FILL_THROUGH),
            ('бот, налив насквозь', no_event(broker), FILL_THROUGH),
        ]
        share = np.mean([not broker_takes(o) for o in orders]) * 100 if orders else 0
        print(f'\n== {period}: пар {len(data)}, заявок ИИ {len(orders)}, брокер отказал бы {share:.0f}%', flush=True)
        for name, pool, through in variants:
            smc_engine.FILL_THROUGH_PCT = through
            out = run_portfolio(pool, exec_data, **live)
            smc_engine.FILL_THROUGH_PCT = 0.0
            rs = [t['pnl'] / t['risk'] for t in out['trades']]
            s = compute_stats(out)
            totals.setdefault(name, []).extend(rs)
            print(f'   {name:32s} {len(rs):4d} сд  {sum(rs):+7.1f}R  {np.mean(rs) if rs else 0:+.3f} R/сд  '
                  f'PF {s.get("profit_factor", 0):.2f}  просадка {s.get("max_dd_pct", 0):5.1f}%  '
                  f'доходность {s.get("total_return_pct", s.get("return_pct", 0)):+.1f}%', flush=True)
            if name == 'ИИ: правила, без календаря':
                stop_of = {o.key: o.meta['stop_pct'] for o in pool}
                line = []
                for lo, hi in STOP_BUCKETS:
                    got = [t['pnl'] / t['risk'] for t in out['trades'] if lo <= stop_of[t['key']] < hi]
                    buckets[(lo, hi)].extend(got)
                    line.append(f'{lo:.2f}–{hi if hi < 99 else "…"}%: {len(got)} сд {sum(got):+.1f}R')
                print('      по стопу — ' + ' | '.join(line), flush=True)
    print('\nВСЕ ПЯТЬ ПЕРИОДОВ (12m и fresh частично перекрываются):')
    for name, rs in totals.items():
        r = np.array(rs)
        lo, hi = ci(r) if len(r) else (0.0, 0.0)
        print(f'   {name:32s} {len(r):4d} сд  {r.sum():+7.1f}R  {r.mean() if len(r) else 0:+.3f} R/сд '
              f'[{lo:+.3f}; {hi:+.3f}]')
    print('\nСДЕЛКИ «ПРАВИЛА, БЕЗ КАЛЕНДАРЯ» ПО ШИРИНЕ СТОПА:')
    for (lo, hi), rs in buckets.items():
        r = np.array(rs)
        if len(r):
            a, b = ci(r)
            print(f'   стоп {lo:.2f}–{hi if hi < 99 else "…"}%: {len(r):4d} сд  {r.sum():+7.1f}R  '
                  f'{r.mean():+.3f} R/сд [{a:+.3f}; {b:+.3f}]  в плюс {np.mean(r > 0) * 100:.0f}%')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
