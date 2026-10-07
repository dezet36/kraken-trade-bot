"""
ФИБО на 2021 годе — проверка N1–N3 аудита сделок 07.10.2026
(docs/Аудит_сделок_2026-10-07.md, раздел 10; протокол закоммичен до
генерации заявок, 4957ac8).

Заявки — боевой сканер (fibo_live_sim.py backtest_cache_y21b →
results/fibo_live_orders_backtest_cache_y21b.pkl), Binance UM, 10 пар;
решения 2021 года (постановка 01.01.2021 – 01.01.2022). Портфель, тренд 4ч и
фандинг — как в fibo_htf_split.py.

    база  F3 — шорты против толпы (как в боте)
    N1    F3 и тренд 4ч NEUTRAL
    N2    F3 без безубытка на уровне B
    N3    N1 и N2 вместе
Приёмка каждого: R на сделку выше базы И среднее > 0 при наливе «насквозь».

    python research/fibo_y21_check.py   → results/fibo_y21_check.txt
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fibo_live_sim as FS                            # noqa: E402
import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
import smc_engine                                     # noqa: E402
from common import ci                                 # noqa: E402
from fibo_crowd import funding_at                     # noqa: E402
from fibo_htf_split import BASE, htf_for, no_be      # noqa: E402

CACHE = 'backtest_cache_y21b'
START, END = pd.Timestamp('2021-01-01'), pd.Timestamp('2022-01-01')

RULES = [
    ('база F3 — шорты против толпы (как в боте)', lambda o, f, t: o.direction == 'SHORT' and f <= 0, False),
    ('N1 F3 и тренд 4ч NEUTRAL', lambda o, f, t: o.direction == 'SHORT' and f <= 0 and t == 'NEUTRAL', False),
    ('N2 F3 без безубытка на уровне B', lambda o, f, t: o.direction == 'SHORT' and f <= 0, True),
    ('N3 NEUTRAL и без безубытка', lambda o, f, t: o.direction == 'SHORT' and f <= 0 and t == 'NEUTRAL', True),
    ('живая ФИБО обе стороны, без толпы (для понимания)', lambda o, f, t: True, False),
    ('F3 и тренд 4ч BEARISH (для понимания)', lambda o, f, t: o.direction == 'SHORT' and f <= 0 and t == 'BEARISH', False),
    ('лонги (для понимания)', lambda o, f, t: o.direction == 'LONG', False),
]


def main():
    with open(os.path.join(HERE, 'results', f'fibo_live_orders_{CACHE}.pkl'), 'rb') as fh:
        orders = pickle.load(fh)
    orders = [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT
              and START <= pd.Timestamp(o.created).tz_localize(None) < END]
    bt.CACHE_DIR = os.path.join(HERE, CACHE)
    pairs = sorted({o.pair for o in orders})
    data = {p: d for p in pairs if (d := bt.load_pair(p)) is not None}
    exec_data = {p: d['5m'] for p, d in data.items()}
    fund = {p: S.load_series(CACHE, 'funding', p, 'funding_rate') for p in pairs}
    trend, signed = {}, {}
    for p in pairs:
        trend.update(htf_for([o for o in orders if o.pair == p], data[p]))
    for o in orders:
        t = int(pd.Timestamp(o.created).tz_localize('UTC').value // 10 ** 6)
        signed[id(o)] = funding_at(fund.get(o.pair), t) * (1.0 if o.direction == 'LONG' else -1.0)
    mix = pd.Series([trend[id(o)] + '/' + o.direction for o in orders]).value_counts().to_dict()
    nofund = sum(1 for o in orders if not np.isfinite(signed[id(o)]))
    print(f'2021, Binance UM, пар {len(pairs)}, заявок в пределе издержек {len(orders)} (без ставки {nofund}); {mix}')
    res = {}
    for name, rule, drop_be in RULES:
        pool = [o for o in orders if rule(o, signed[id(o)], trend[id(o)])]
        if drop_be:
            pool = no_be(pool)
        cells = []
        for through in (0.0, 0.0005):
            smc_engine.FILL_THROUGH_PCT = through
            try:
                out = smc_engine.run_portfolio(pool, exec_data, **BASE)
            finally:
                smc_engine.FILL_THROUGH_PCT = 0.0
            rs = np.array([t['pnl'] / t['risk'] for t in out['trades']])
            res[(name, through)] = rs
            lo, hi = ci(rs) if len(rs) > 2 else (np.nan, np.nan)
            dd = smc_engine.compute_stats(out).get('max_dd_pct', 0.0)
            cells.append(f'{"касание" if not through else "насквозь"} {len(rs):4d} сд {rs.sum():+7.1f}R '
                         f'{rs.mean() if len(rs) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}] просадка {dd:.0f}%')
        print(f'  {name:50s} ' + ' | '.join(cells), flush=True)
    base = res[(RULES[0][0], 0.0)]
    print('\nПРИЁМКА (база — F3 на 2021)')
    for name, _, _ in RULES[1:4]:
        r0, r1 = res[(name, 0.0)], res[(name, 0.0005)]
        better = len(r0) and r0.mean() > base.mean()
        positive = len(r1) and r1.mean() > 0
        print(f'  {name}: {r0.mean():+.3f} против базы {base.mean():+.3f} — {"выше" if better else "не выше"}; '
              f'насквозь {r1.mean():+.3f} — {"> 0" if positive else "≤ 0"} → '
              f'{"ПРИНЯТ" if better and positive else "не принят"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
