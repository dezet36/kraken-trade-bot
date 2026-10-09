"""
Фибо «против толпы» — перенос правила, принятого у SMC 27.09.2026.

У SMC единственное прошедшее приёмку правило — брать сетап, только если
толпа не стоит в сторону сделки (фандинг не в пользу толпы). Фибо на истории
в точной симуляции теряет во всех периодах (research/fibo_live_sim.py:
bear −148.4R, mid1 −13.7R, mid2 −27.3R, 12m −13.5R). Каждая заявка отдельно
(ai_filter_study, скелет fibo): толпа за сделку −0.069R [−0.096; −0.043],
против — +0.033R [+0.001; +0.065], против лучше во всех 4 периодах; но у
лонгов против толпы хуже (−0.160R), плюс дают шорты.

Правила:
    F1  против толпы (как у SMC) — ЗАПИСАНО ДО ПОРТФЕЛЬНОГО ЗАМЕРА;
    F2  только шорты                   — ПОСЛЕ просмотра разбивки по сторонам;
    F3  только шорты против толпы      — ПОСЛЕ просмотра.
Приёмка F1: итог лучше живой фибо в каждом из четырёх периодов и
неотрицательный в сумме, в том числе при наливе «насквозь». F2 и F3
показаны для понимания; принимать их по этим же данным нельзя.

Заявки — боевой сканер фибо на 5-минутных шагах (results/fibo_live_orders_*.pkl),
портфель — правила брокера для фибо (как fibo_live_sim.report): предел
издержек 5%, снятие у цели, срок 72 ч (в заявке), безубыток на уровне B (в
заявке), пауза 12 ч и с постановки, заявка занимает пару, без кэпа и слотов.
Фандинг — последняя выплаченная ставка на момент заявки.

Запуск:
    python research/fibo_crowd.py
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
os.environ.setdefault('BOT_DATA_DIR', os.path.join(HERE, 'results', 'sim-data'))

from infra import logger  # noqa: E402
logger.log = lambda *a, **k: None

import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
import smc_engine                                     # noqa: E402
from strategies import strategy_profile  # noqa: E402
from common import ci                                 # noqa: E402
from smc_engine import compute_stats, run_portfolio   # noqa: E402

CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m'}
COST_LIMIT = 5.0


def funding_at(series, t_ms):
    if series is None:
        return np.nan
    ts, values = series
    k = int(np.searchsorted(ts, t_ms, side='right')) - 1
    return values[k] * 1e4 if k >= 0 else np.nan


def main():
    base = dict(risk_pct=bt.RISK_PCT, max_positions=99, cooldown_hours=strategy_profile.cooldown_hours('FIBO'),
                breakeven_after_tp1=False, max_hold_hours=strategy_profile.max_hold_hours('FIBO') or 336.0,
                max_same_direction=0, occupy_while_pending=True, cooldown_from_placement=True,
                cancel_at_target=True)
    totals = {}
    rules = [
        ('живая фибо', lambda o, f: True),
        ('F1 против толпы', lambda o, f: f <= 0),
        ('F2 только шорты (после просмотра)', lambda o, f: o.direction == 'SHORT'),
        ('F3 шорты против толпы (после просмотра)', lambda o, f: o.direction == 'SHORT' and f <= 0),
        ('F4 шорты при ставке от середины +1 б.п. (второй круг)', lambda o, f: o.direction == 'SHORT' and not (f > -1.0)),
        ('F5 против толпы от середины, обе стороны (второй круг)', lambda o, f: not (f > -1.0)),
    ]
    for period, cache in CACHES.items():
        with open(os.path.join(HERE, 'results', f'fibo_live_orders_{cache}.pkl'), 'rb') as fh:
            orders = pickle.load(fh)
        orders = [o for o in orders if o.meta['cost_share'] <= COST_LIMIT]
        bt.CACHE_DIR = os.path.join(HERE, cache)
        exec_data = {p: d['5m'] for p in bt.DEFAULT_PAIRS if (d := bt.load_pair(p)) is not None}
        fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in bt.DEFAULT_PAIRS}
        signed = {}
        for o in orders:
            t = int(pd.Timestamp(o.created).value // 10 ** 6)
            side = 1.0 if o.direction == 'LONG' else -1.0
            signed[id(o)] = funding_at(fund.get(o.pair), t) * side
        line = []
        for name, rule in rules:
            for through in (0.0, 0.0005):
                pool = [o for o in orders if rule(o, signed[id(o)])]
                smc_engine.FILL_THROUGH_PCT = through
                try:
                    out = run_portfolio(pool, exec_data, **base)
                finally:
                    smc_engine.FILL_THROUGH_PCT = 0.0
                rs = [t['pnl'] / t['risk'] for t in out['trades']]
                s = compute_stats(out)
                key = name + (', налив насквозь' if through else '')
                totals.setdefault(key, {})[period] = (len(rs), float(np.sum(rs)), s.get('max_dd_pct', 0.0), rs)
            line.append(f"{name}: {totals[name][period][0]} сд {totals[name][period][1]:+.1f}R")
        print(f'{period:5s} ' + ' | '.join(line), flush=True)
    print('\nИТОГ ПО ЧЕТЫРЁМ ПЕРИОДАМ (итог R; просадка счёта при риске 1%)')
    for key, per in totals.items():
        allr = np.concatenate([np.array(v[3]) for v in per.values()])
        lo, hi = ci(allr)
        print(f'  {key:48s} {len(allr):5d} сд {allr.sum():+8.1f}R {allr.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]  '
              + ' '.join(f'{p} {per[p][1]:+6.1f}/{per[p][2]:.0f}%' for p in CACHES))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
