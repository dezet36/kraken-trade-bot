"""
Сетапы smart money на нескольких таймфреймах: 15м, 1ч, 4ч — есть ли плюс у
каждого потока ДО того, как модель будет их одобрять.

ЗАЧЕМ. Предложение владельца 27.09.2026: искать сетапы SMC на 15м, 1ч и 4ч
(сетапов больше), а модель ИИ говорит по каждому «да» или «нет». Фильтр не
делает убыточный поток прибыльным, если у фильтра нет навыка, — а опыт A
показал, что своё суждение модели обратно исходу. Поэтому сначала — сами
потоки: правила отбора ИИ (llm_rules.DECISION, копия SMC) на каждом
таймфрейме, пул llm_rules.POOL, живые правила брокера, предел издержек 10%
(доля круга 0.075% в риске — на 15м стопы теснее, и предел отсекает больше).

Раскладки кадров (зоны — рабочий ТФ; слом и направление — старше):
    15м-А: зоны 15м, слом 1ч, направление 4ч    (всё на ступень ниже 1ч)
    15м-Б: зоны 15м, слом 4ч, направление день   (старшие как у боевой SMC)
    1ч:    зоны 1ч,  слом 4ч, направление день   (боевая SMC — сверка)
    4ч-А:  зоны 4ч,  слом день, направление неделя
    4ч-Б:  зоны 4ч,  слом день, направление день
Срок заявки — 48 свечей своего ТФ (у 1ч это 48 ч, как у SMC).

Запуск:
    python research/ai_mtf_smc.py
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
from common import ci                                 # noqa: E402
from smc import signal as smc_signal                  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402

R = llm_rules.DECISION
COST_ROUND_TRIP_PCT = 0.075
LAYOUTS = {
    '15м-А': ('15m', '1h', '4h'),
    '15м-Б': ('15m', '4h', '1d'),
    '1ч': ('1h', '4h', '1d'),
    '4ч-А': ('4h', '1d', '1w'),
    '4ч-Б': ('4h', '1d', '1d'),
}
BAR_H = {'15m': 0.25, '1h': 1.0, '4h': 4.0}


def frame(data, tf):
    if tf == '1w':
        return bt.resample(data['1h'], '1W')
    return data[tf]


def orders_for(pair, data, layout):
    poi_tf, htf_tf, bias_tf = LAYOUTS[layout]
    frames = {'bias': frame(data, bias_tf), 'htf': frame(data, htf_tf), 'poi': frame(data, poi_tf)}
    ctx = smc_signal.build_context(frames, pair=pair)
    df = frames['poi']
    bar = np.timedelta64(int(BAR_H[poi_tf] * 3600), 's')
    expiry = np.timedelta64(int(48 * BAR_H[poi_tf] * 3600), 's')
    out, seen = [], set()
    for i in range(60, len(df)):
        setup, _why = ctx.evaluate(i, balance=bt.INITIAL_BALANCE, decision=R)
        if setup is None:
            continue
        poi = setup['poi']
        key = (pair, layout, poi['type'], poi['index'], setup['direction'])
        if key in seen:
            continue
        seen.add(key)
        trade = setup['params']
        stop_pct = abs(trade['entry'] - trade['stop_loss']) / trade['entry'] * 100
        if COST_ROUND_TRIP_PCT / stop_pct * 100 > R.MAX_ENTRY_COST_SHARE_PCT:
            continue                                   # брокер отказал бы: «предел издержек»
        created = np.datetime64(pd.Timestamp(setup['time']).tz_convert('UTC').tz_localize(None)) + bar
        out.append(Order(pair=pair, direction=setup['direction'], entry=trade['entry'],
                         stop=trade['stop_loss'], targets=trade['targets'], fractions=trade['fractions'],
                         created=created, expires=created + expiry, key=key,
                         meta={'layout': layout, 'stop_pct': stop_pct}))
    return out


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
        exec_data = {p: f['5m'] for p, f in data.items()}
        by_layout = {}
        for layout in LAYOUTS:
            orders = []
            for pair, frames in data.items():
                orders += orders_for(pair, frames, layout)
            by_layout[layout] = orders
        print(f'\n== {period}: пар {len(data)}', flush=True)
        runs = [(name, orders) for name, orders in by_layout.items()]
        runs.append(('все ТФ одним счётом (15м-Б + 1ч + 4ч-Б)',
                     by_layout['15м-Б'] + by_layout['1ч'] + by_layout['4ч-Б']))
        for name, orders in runs:
            out = run_portfolio(orders, exec_data, **live)
            rs = [t['pnl'] / t['risk'] for t in out['trades']]
            s = compute_stats(out)
            totals.setdefault(name, []).extend(rs)
            stop = np.median([o.meta['stop_pct'] for o in orders]) if orders else 0
            print(f'   {name:40s} заявок {len(orders):5d}  сделок {len(rs):4d}  {sum(rs):+7.1f}R  '
                  f'{np.mean(rs) if rs else 0:+.3f} R/сд  PF {s.get("profit_factor", 0):.2f}  '
                  f'просадка {s.get("max_dd_pct", 0):5.1f}%  стоп медиана {stop:.2f}%', flush=True)
    print('\nВСЕ ПЯТЬ ПЕРИОДОВ (12m и fresh частично перекрываются):')
    for name, rs in totals.items():
        r = np.array(rs)
        if len(r):
            lo, hi = ci(r)
            print(f'   {name:40s} {len(r):5d} сд  {r.sum():+8.1f}R  {r.mean():+.3f} R/сд [{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
