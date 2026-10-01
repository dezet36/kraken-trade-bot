"""
Разбор SMC 01.10.2026: портфель с правилами живого брокера (кэп 3 в сторону,
пауза 12 ч с постановки, заявка 48 ч, удержание до 336 ч) на сетапах «глазами
бота», 10 пар пула, пять периодов. Налив касанием и насквозь 0.05%.

Варианты — только то, что прошло приёмку в smcr_eval (и базы для сравнения):
    python research/smcr_portfolio.py base          # ядро и живая SMC
    python research/smcr_portfolio.py D1             # + снятие заявки при потере направления
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smcr_live as L                                 # noqa: E402
from common import ci                                 # noqa: E402

E = L.E


def crowd_ok(r, f):
    return not ((f or {}).get('funding', np.nan) > -1.0)


END_12M = np.datetime64('2026-07-04T23:00')


def run(label, crowd, cancel=False, ft=0.0, dedup=False):
    """dedup — fresh только после конца 12m (периоды пересекаются на 8 месяцев)."""
    spec = dict(E.LIVE, pairs=L.POOL10, layouts=('live',), filt=crowd_ok if crowd else (lambda r, f: True))
    per, allr = {}, []
    for period in E.PERIODS:
        orders = E.orders_for(period, spec)
        if dedup and period == 'fresh':
            orders = [o for o in orders if o.created > END_12M]
        if cancel:
            fn = L.cancel_on_bias(period)
            out = []
            for o in orders:
                ct = fn(o)
                if ct is not None and ct < o.expires:
                    o = E.Order(pair=o.pair, direction=o.direction, entry=o.entry, stop=o.stop, targets=o.targets,
                                fractions=o.fractions, created=o.created, expires=ct, key=o.key, meta=o.meta)
                out.append(o)
            orders = out
        data = E.exec_data(period, spec['pairs'])
        E.smc_engine.FILL_THROUGH_PCT = ft
        try:
            res = E.run_portfolio(orders, data, risk_pct=1.0, max_positions=spec['max_positions'],
                                  cooldown_hours=spec['cooldown'], breakeven_after_tp1=spec['breakeven'],
                                  max_hold_hours=spec['max_hold'], max_same_direction=spec['cap'],
                                  occupy_while_pending=spec['occupy'], cooldown_from_placement=spec['cool_from_place'],
                                  cancel_at_target=spec['cancel_at_target'])
        finally:
            E.smc_engine.FILL_THROUGH_PCT = 0.0
        rs = [t['pnl'] / t['risk'] for t in res['trades']]
        per[period] = (len(rs), float(np.sum(rs)) if rs else 0.0)
        allr += rs
    r = np.array(allr)
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    print(f'  {label:46s} {len(r):4d} сд {r.sum():+7.1f}R {r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}]  '
          + ' '.join(f'{p} {per[p][1]:+6.1f}' for p in E.PERIODS), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    which = sys.argv[1:] or ['base']
    print('портфель с правилами брокера, 10 пар пула, R; периоды: ' + ', '.join(E.PERIODS))
    for crowd in (False, True):
        tag = 'живая SMC (фильтр толпы)' if crowd else 'ядро без фильтра толпы'
        for ft in (0.0, L.FT):
            suffix = ', насквозь' if ft else ''
            if 'base' in which:
                run(f'{tag}{suffix}', crowd, ft=ft)
                run(f'{tag}{suffix}, без двойного счёта', crowd, ft=ft, dedup=True)
            if 'D1' in which:
                run(f'{tag} + D1{suffix}', crowd, cancel=True, ft=ft)
