"""
Уровни «против толпы» — перенос правила, принятого у SMC 27.09.2026
(лонг только при фандинге ≤ 0, шорт при ≥ 0).

Заявки — точная симуляция боевого сканера уровней (research/levels_live_sim.py,
results/levels_live_orders_<период>.pkl, заявки «как в бою»), портфель — те же
правила живого брокера, что в levels_live_sim.portfolio. Фандинг — последняя
выплаченная ставка на момент заявки.

Приёмка — как у фибо (research/fibo_crowd.py): итог лучше живых уровней в
каждом периоде и неотрицательный в сумме, в том числе при наливе «насквозь».
Сделок у уровней мало (44–201 за период) — вывод будет слабее, чем у SMC.

Запуск:
    python research/levels_crowd.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_setups as S                                 # noqa: E402
import levels_live_sim as L                           # noqa: E402
import smc_engine                                     # noqa: E402
from common import ci                                 # noqa: E402
from smc_engine import compute_stats                  # noqa: E402


def funding_at(series, t_ms):
    if series is None:
        return np.nan
    ts, values = series
    k = int(np.searchsorted(ts, t_ms, side='right')) - 1
    return values[k] * 1e4 if k >= 0 else np.nan


def main():
    rules = [('живые уровни', lambda o, f: True), ('против толпы', lambda o, f: f <= 0)]
    totals = {}
    for period in ('bear', '12m', 'fresh'):
        cache, _attr = L.PERIODS[period]
        got = L.build(period)
        exec_data = {}
        for pair in got:
            _h, m5 = L.load(cache, pair)
            if m5 is not None:
                exec_data[pair] = m5
        orders = [o for pair in got for o in got[pair][1]]
        fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in got}
        signed = {}
        for o in orders:
            t = int(pd.Timestamp(o.created).value // 10 ** 6)
            side = 1.0 if o.direction in ('LONG', 'BULLISH') else -1.0
            signed[id(o)] = funding_at(fund.get(o.pair), t) * side
        known = sum(np.isfinite(v) for v in signed.values())
        line = []
        for name, rule in rules:
            for through in (0.0, 0.0005):
                pool = [o for o in orders if rule(o, signed[id(o)])]
                smc_engine.FILL_THROUGH_PCT = through
                try:
                    res = L.portfolio(pool, exec_data)
                finally:
                    smc_engine.FILL_THROUGH_PCT = 0.0
                rs = [t['pnl'] / t['risk'] for t in res['trades'] if t.get('risk')]
                key = name + (', налив насквозь' if through else '')
                totals.setdefault(key, {})[period] = (len(rs), float(np.sum(rs)),
                                                      compute_stats(res).get('max_dd_pct', 0.0), rs)
            line.append(f"{name}: {totals[name][period][0]} сд {totals[name][period][1]:+.1f}R")
        print(f'{period:5s} заявок {len(orders)}, с фандингом {known} | ' + ' | '.join(line), flush=True)
    print('\nИТОГ (итог R; просадка счёта)')
    for key, per in totals.items():
        allr = np.concatenate([np.array(v[3]) for v in per.values()])
        lo, hi = ci(allr) if len(allr) > 10 else (np.nan, np.nan)
        print(f'  {key:34s} {len(allr):4d} сд {allr.sum():+7.1f}R {allr.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]  '
              + ' '.join(f'{p} {per[p][1]:+6.1f}/{per[p][2]:.0f}%' for p in per))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
