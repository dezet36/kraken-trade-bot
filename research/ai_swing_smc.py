"""
SMC по глобальной структуре: зоны на 4ч, старший порядок — день и неделя.

ЗАЧЕМ. Вопрос владельца 26.09.2026: почему ИИ не торгует от снятия ликвидности
по ГЛОБАЛЬНОЙ структуре. Боевое ядро SMC (smc/) не привязано к часовику:
MarketContext берёт любые кадры {'bias', 'htf', 'poi'}. Здесь те же правила
решений SMC (smc/params — нетронутый ордер-блок в дисконте/премии, конфлюенс
≥ 4.5, R:R ≥ 4, стоп за краем блока, Фибо-цели 25/25/50) применены на шаг
выше: зоны — на 4ч, слом — по дню, направление — по неделе.

Что ожидается и почему может сработать: ход от зоны 4ч крупнее, а круг
издержек тот же 0.075–0.11%, значит их доля в риске ниже (память проекта:
«издержки решают всё» — доля = ставка ÷ дистанция стопа).

Параметры решений — как у боевой SMC, без подбора; меняется только кадр и
срок заявки (48 ч как у SMC и 120 ч — зона 4ч живёт дольше часовой).
Портфель — живые правила SMC (research/broker_rules.py).

Запуск:
    python research/ai_swing_smc.py
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
from smc import params as smc_params, signal as smc_signal  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402

LIVE = dict(occupy_while_pending=True, cooldown_from_placement=True)
POI_BAR = np.timedelta64(4 * 3600, 's')


def swing_orders(pair, data, ttl_h):
    frames = {'bias': bt.resample(data['1h'], '1W'), 'htf': data['1d'], 'poi': data['4h']}
    ctx = smc_signal.build_context(frames, pair=pair)
    df = frames['poi']
    orders, seen = [], set()
    expiry = np.timedelta64(int(ttl_h * 3600), 's')
    for i in range(30, len(df)):
        setup, _why = ctx.evaluate(i, balance=bt.INITIAL_BALANCE)
        if setup is None:
            continue
        poi = setup['poi']
        key = (pair, poi['type'], poi['index'], setup['direction'])
        if key in seen:
            continue
        seen.add(key)
        created = np.datetime64(pd.Timestamp(setup['time']).tz_convert('UTC').tz_localize(None)) + POI_BAR
        trade = setup['params']
        orders.append(Order(pair=pair, direction=setup['direction'], entry=trade['entry'],
                            stop=trade['stop_loss'], targets=trade['targets'], fractions=trade['fractions'],
                            created=created, expires=created + expiry, key=key,
                            meta={'confluence': setup['confluence'], 'rr': trade['rr']}))
    return orders


def main():
    base = dict(max_positions=bt.MAX_POSITIONS, cooldown_hours=smc_params.COOLDOWN_HOURS,
                breakeven_after_tp1=smc_params.BREAKEVEN_AFTER_TP1,
                max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS,
                max_same_direction=smc_params.MAX_SAME_DIRECTION, risk_pct=bt.RISK_PCT, **LIVE)
    pools = {'10 пар SMC': bt.DEFAULT_PAIRS,
             'другие 10 пар': [p for p in D.PAIRS if p not in bt.DEFAULT_PAIRS]}
    totals = {}
    for period, cache in D.PERIODS.items():
        bt.CACHE_DIR = os.path.join(HERE, cache)
        print(f'\n== {period}', flush=True)
        for pool_name, pairs in pools.items():
            data = {p: d for p in pairs if (d := bt.load_pair(p)) is not None}
            if not data:
                continue
            exec_data = {p: f['5m'] for p, f in data.items()}
            for ttl in (48, 120):
                orders = []
                for pair, frames in data.items():
                    orders += swing_orders(pair, frames, ttl)
                out = run_portfolio(orders, exec_data, **base)
                rs = [t['pnl'] / t['risk'] for t in out['trades']]
                s = compute_stats(out)
                label = f'{pool_name}, заявка {ttl} ч'
                totals.setdefault(label, []).extend(rs)
                stop_pct = np.median([abs(o.entry - o.stop) / o.entry * 100 for o in orders]) if orders else 0
                print(f'   {label:28s} заявок {len(orders):4d}  сделок {len(rs):4d}  {sum(rs):+7.1f}R  '
                      f'{np.mean(rs) if rs else 0:+.3f} R/сд  PF {s.get("profit_factor", 0):.2f}  '
                      f'просадка {s.get("max_dd_pct", 0):5.1f}%  стоп медиана {stop_pct:.2f}%', flush=True)
    print('\nВСЕ ПЯТЬ ПЕРИОДОВ (12m и fresh частично перекрываются):')
    for label, rs in totals.items():
        r = np.array(rs)
        if len(r):
            lo, hi = ci(r)
            print(f'   {label:28s} {len(r):4d} сд  {r.sum():+7.1f}R  {r.mean():+.3f} R/сд [{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
