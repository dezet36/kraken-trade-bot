"""
SMC без фандинга, разведка — второй шаг (30.09.2026): режим волатильности.

Разведка (results/smc_explore.txt) нашла одно правило, которое держится в
обеих половинах истории и не разваливается на 23 новых парах: волатильность
пары в верхней части месяца (ранг ATR ≥ 75). Правило подобрано ПОСЛЕ
просмотра разбивок, поэтому здесь не приёмка, а проверка на излом:
  1. порог 50 / 60 / 75 / 90 — эффект не должен держаться на одной отметке;
  2. портфель «как бот» по периодам, касанием и насквозь — 20 пар и 23 новые;
  3. вместе с фильтром толпы и без него.
Довод из логики SMC: блок работает, когда рынок в «смещении» (displacement) —
идёт широкими свечами; в тихом рынке реакция от блока слабая, а дальние цели
Фибо не достигаются.

Запуск:
    python research/smc_explore2.py > research/results/smc_explore2.txt
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_improve as I                               # noqa: E402  (fresh — 20 пар)
from common import ci                                 # noqa: E402
from smc_audit_targets import orders_retry            # noqa: E402

E = I.E
FT = 0.0005


def g(x, key):
    return (x or {}).get(key, np.nan)


def vol(limit):
    return lambda r, x: g(x, 'atr_rank') >= limit


def crowd(r, x):
    return not (g(x, 'funding') > -1.0)


def run(layout, pairs, periods, filt, label, ft=0.0):
    spec = dict(E.LIVE, pairs=pairs, layouts=(layout,), filt=filt, fill_through=ft)
    per, allr = {}, []
    for period in periods:
        res, _ = E.portfolio(period, spec, orders=orders_retry(period, spec, layout))
        rs = [t['pnl'] / t['risk'] for t in res['trades']]
        per[period] = (len(rs), sum(rs))
        allr += rs
    r = np.array(allr)
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    print(f'  {label:44s} {len(r):4d} сд, в плюс {np.mean(r > 0) if len(r) else 0:4.0%}, '
          f'{r.mean() if len(r) else 0:+.3f}R [{lo:+.3f}; {hi:+.3f}], итог {r.sum():+7.1f}R  '
          + ' '.join(f'{p} {per[p][1]:+6.1f}' for p in periods), flush=True)


def main():
    pairs20 = tuple(I.D.PAIRS)
    x12 = tuple(sorted(set(E.rows('12m', 'live12x')['pair'])))
    print('1. ПОРТФЕЛЬ «КАК БОТ», 20 ПАР, 5 ПЕРИОДОВ')
    for filt, label in ((None, 'база (без фандинга)'), (vol(50), 'волатильность ≥ 50'),
                        (vol(60), 'волатильность ≥ 60'), (vol(75), 'волатильность ≥ 75'),
                        (vol(90), 'волатильность ≥ 90'), (crowd, 'фильтр толпы (как бот)'),
                        (lambda r, x: vol(75)(r, x) and crowd(r, x), 'волатильность ≥ 75 и толпа')):
        run('live20', pairs20, E.PERIODS, filt, label)
        run('live20', pairs20, E.PERIODS, filt, '   … насквозь', FT)
    print('\n2. 23 НОВЫЕ ПАРЫ 12m (в подборе не участвовали)')
    for filt, label in ((None, 'база'), (vol(50), 'волатильность ≥ 50'), (vol(75), 'волатильность ≥ 75'),
                        (vol(90), 'волатильность ≥ 90')):
        run('live12x', x12, ('12m',), filt, label)
        run('live12x', x12, ('12m',), filt, '   … насквозь', FT)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
