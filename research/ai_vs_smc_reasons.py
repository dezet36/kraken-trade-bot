"""
Какое правило ИИ отсекает сетапы SMC и чего стоят отсечённые (к research/ai_vs_smc.py).

По каждому сетапу SMC: где его вход относительно последнего HL/LH часа (по какую
сторону слома), какой вышел бы стоп ИИ, и итог сетапа по правилам SMC. Плюс
замены, которым стоп ИИ не нужен, — на ВСЕХ сетапах: цели ИИ при стопе SMC,
сопровождение ИИ, срок заявки 12 ч.

Запуск:
    python research/ai_vs_smc_reasons.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_vs_smc as V                                  # noqa: E402
from ai_vs_smc import D, bt, smc_params, _prepare, simulate_order, variant, ai_targets, H  # noqa: E402
from common import ci                                 # noqa: E402


def classify(snap, long_, entry):
    ref = snap['HL' if long_ else 'LH']
    if ref is None:
        return 'нет HL/LH часа'
    a = snap['atr'] if np.isfinite(snap['atr']) else 0.0
    buf = (D.STOP_HUNT_BASE_PCT + D.STOP_HUNT_ATR_SHARE * a) / 100
    stop = ref['price'] * (1 - buf) if long_ else ref['price'] * (1 + buf)
    if (stop >= entry) if long_ else (stop <= entry):
        return 'вход за сломом часа (по мёртвую сторону)'
    if abs(entry - stop) / entry * 100 < max(D.MIN_STOP_COST_PCT, D.STOP_ATR_SHARE * a):
        return 'стоп за сломом теснее 1.5%'
    return 'стоп ИИ есть'


def main():
    rows = []
    for period, cache in D.PERIODS.items():
        bt.CACHE_DIR = os.path.join(HERE, cache)
        for pair in bt.DEFAULT_PAIRS:
            frames = bt.load_pair(pair)
            d = D.prepare(cache, pair)
            if frames is None or d is None:
                continue
            orders = bt.smc_orders(pair, frames)
            idx = {o.key: int(np.searchsorted(d['a1'][:, 0], int(pd.Timestamp(o.created).value // 10 ** 6) - H,
                                              side='right')) - 1 for o in orders}
            snaps = V.snapshots(d, set(idx.values()))
            ex = _prepare(frames['5m'])

            def run(order, be_after_tp1=False, cancel=False):
                start = int(np.searchsorted(ex['ts'], order.created))
                res = simulate_order(order, ex, start, 100.0, breakeven_after_tp1=be_after_tp1,
                                     max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS, cancel_at_target=cancel)
                return None if res is None else res['pnl'] / res['risk']

            for o in orders:
                base = run(o)
                if base is None:
                    continue
                snap = snaps.get(idx[o.key])
                long_ = o.direction in ('BULLISH', 'LONG')
                why = classify(snap, long_, o.entry) if snap else 'нет данных'
                tg = ai_targets(snap, long_, o.entry, o.stop) if snap else None
                rows.append({
                    'period': period, 'why': why, 'smc': base,
                    'stop_pct': abs(o.entry - o.stop) / o.entry * 100,
                    'ai_targets': run(variant(o, targets=tg, fractions=[0.5, 0.5] if len(tg) == 2 else [1.0]))
                    if tg else np.nan,
                    'ai_manage': run(variant(o, be_1r=True, ttl_h=12), be_after_tp1=True, cancel=True),
                    'ttl12': run(variant(o, ttl_h=12)),
                })
        print(f'  {period} готов', flush=True)
    f = pd.DataFrame(rows)
    f.to_pickle(os.path.join(HERE, 'results', 'ai_vs_smc_rows.pkl'))
    print(f'\nСделок SMC (каждая заявка отдельно, 10 пар боевого пула, 5 периодов): {len(f)}, '
          f'{f.smc.mean():+.3f} R/сд')
    print('\n1. ГДЕ ВХОД SMC ОТНОСИТЕЛЬНО СЛОМА ЧАСОВОЙ СТРУКТУРЫ (правило стопа ИИ)')
    for why, g in f.groupby('why'):
        lo, hi = ci(g.smc.to_numpy())
        print(f'   {why:44s} {len(g):5d} ({len(g) / len(f) * 100:3.0f}%)  итог у SMC {g.smc.mean():+.3f} R/сд '
              f'[{lo:+.3f}; {hi:+.3f}], стоп SMC медиана {g.stop_pct.median():.2f}%')
    order = ['вход за сломом часа (по мёртвую сторону)', 'стоп за сломом теснее 1.5%', 'стоп ИИ есть']
    print('\n   по периодам, R/сд у SMC (число сделок):')
    for p, g in f.groupby('period', sort=False):
        cells = [f'{g[g.why == w].smc.mean():+.3f} ({int((g.why == w).sum())})' for w in order]
        print(f'   {p:6s} мёртвая сторона {cells[0]:>15s} | теснее 1.5% {cells[1]:>15s} | '
              f'законные для ИИ {cells[2]:>15s}')
    print('\n2. ЗАМЕНЫ БЕЗ СТОПА ИИ — на тех же сделках (там, где замена определена)')
    for col, name in (('ai_targets', 'цели ИИ: ближняя ликвидность ≥2R, 50/50'),
                      ('ai_manage', 'сопровождение ИИ: БУ +1R, снятие у цели, 12 ч'),
                      ('ttl12', 'только срок заявки 12 ч')):
        g = f[f[col].notna()]
        dlt = (g[col] - g.smc).to_numpy()
        lo, hi = ci(dlt)
        print(f'   {name:44s} {len(g):5d} сд: SMC {g.smc.mean():+.3f} → {g[col].mean():+.3f} R/сд, '
              f'разность {dlt.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]')
    print('\n   по периодам (SMC → цели ИИ | → сопровождение ИИ):')
    for p, g in f.groupby('period', sort=False):
        t = g[g.ai_targets.notna()]
        print(f'   {p:6s} {t.smc.mean():+.3f} → {t.ai_targets.mean():+.3f} | {g.smc.mean():+.3f} → {g.ai_manage.mean():+.3f}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
