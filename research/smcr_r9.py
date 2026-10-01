"""
Разбор SMC, второй круг: R9 (снять заявку, когда толпа перестала быть против)
и R10 (выйти, когда толпа перешла на нашу сторону) — живая SMC с фильтром
толпы, заявки «глазами бота», каждая отдельно (docs/SMC_разбор_аналитика_2026-10-01.md, раздел 7).

    python research/smcr_r9.py     # → results/smcr/eval_r9.txt
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smcr_live as L                                 # noqa: E402
import smcr_eval as V                                 # noqa: E402
from common import ci                                 # noqa: E402

E = L.E
OUT = L.OUT
LIMIT = -1.0                                           # фильтр бота: ставка в сторону сделки ≤ −1 б.п.
FLIP = 1.0                                             # R10: ставка в сторону сделки ≥ +1 б.п.
SAMPLES = (('pool', 'live', L.POOL10), ('other', 'live20', tuple(p for p in L.D.PAIRS if p not in L.POOL10)))


def payments(period, pair):
    got = E.S.load_series(L.D.PERIODS[period], 'funding', pair, 'funding_rate')
    if got is None:
        return None
    ts, vals = got
    return ts.astype('datetime64[ms]'), vals


def signed_bp(o, rate):
    return rate * 1e4 * (1 if o.direction == 'BULLISH' else -1)


def sim(o, arr, ft, max_hold=336.0, expires=None):
    oo = o if expires is None else E.Order(pair=o.pair, direction=o.direction, entry=o.entry, stop=o.stop,
                                           targets=o.targets, fractions=o.fractions, created=o.created,
                                           expires=expires, key=o.key, meta=o.meta)
    start = int(np.searchsorted(arr['ts'], np.datetime64(oo.created), side='right'))
    E.smc_engine.FILL_THROUGH_PCT = ft
    try:
        return E.simulate_order(oo, arr, start, 100.0, breakeven_after_tp1=False,
                                max_hold_hours=max_hold, cancel_at_target=False)
    finally:
        E.smc_engine.FILL_THROUGH_PCT = 0.0


def rr(res):
    return 0.0 if res is None else res['pnl'] / res['risk']


def run():
    recs = []
    for sample, layout, pairs in SAMPLES:
        for period in E.PERIODS:
            spec = L.spec_for(layout, pairs)
            orders = E.orders_for(period, spec)
            data = E.exec_data(period, pairs)
            prep = {p: E._prepare(df) for p, df in data.items()}
            for o in orders:
                feats = o.meta['feats'] or {}
                x = feats.get('funding', np.nan)
                if not np.isfinite(x) or x > LIMIT:
                    continue                                   # не живая SMC
                arr = prep.get(o.pair)
                pay = payments(period, o.pair)
                if arr is None or pay is None:
                    continue
                pts, pvals = pay
                rec = {'sample': sample, 'period': period, 'pair': o.pair, 't': int(o.meta['row'].t),
                       'dir': 1 if o.direction == 'BULLISH' else -1}
                for ft, suf in ((0.0, ''), (L.FT, '_ft')):
                    base = sim(o, arr, ft)
                    rec['r' + suf] = rr(base)
                    rec['filled' + suf] = base is not None
                    # R9: первая выплата после постановки, где фильтр бы не пустил
                    k = int(np.searchsorted(pts, np.datetime64(o.created, 'ms'), side='right'))
                    bad = [j for j in range(k, len(pts)) if pts[j] < o.expires and signed_bp(o, pvals[j]) > LIMIT]
                    if bad:
                        cancel = pts[bad[0]].astype('datetime64[ns]')
                        rec['r9' + suf] = rr(sim(o, arr, ft, expires=cancel))
                    else:
                        rec['r9' + suf] = rec['r' + suf]
                    # R10: выход на первой выплате после входа, где толпа перешла к нам
                    rec['r10' + suf] = rec['r' + suf]
                    if base is not None:
                        entry_t = np.datetime64(base['entry_time'], 'ms')
                        exit_t = np.datetime64(base['exit_time'], 'ms')
                        k = int(np.searchsorted(pts, entry_t, side='right'))
                        flip = [j for j in range(k, len(pts)) if pts[j] <= exit_t and signed_bp(o, pvals[j]) >= FLIP]
                        if flip:
                            hold_h = float((pts[flip[0]] - entry_t) / np.timedelta64(1, 'h'))
                            again = sim(o, arr, ft, max_hold=hold_h)
                            rec['r10' + suf] = rr(again)
                            rec['r10_hit' + suf] = True
                recs.append(rec)
            print(f'  {sample} {period}: живых заявок {sum(1 for r in recs if r["sample"] == sample and r["period"] == period)}',
                  flush=True)
            E._exec.pop(period, None)
    return pd.DataFrame(recs)


def paired(f, a, b):
    d = (f[a] - f[b]).to_numpy(float)
    if len(d) < 5:
        return np.nan, (np.nan, np.nan), len(d)
    boot = np.random.default_rng(20261001).choice(d, size=(5000, len(d)), replace=True).mean(axis=1)
    return d.mean(), tuple(np.percentile(boot, [2.5, 97.5])), len(d)


def report(f):
    pool, other = f[f['sample'] == 'pool'], f[f['sample'] == 'other']
    for name, col in (('R9 снять заявку, когда толпа перестала быть против', 'r9'),
                      ('R10 выйти, когда толпа перешла к нам', 'r10')):
        print(f'\n{name} — парная разница R на заявку (неналитая = 0)')
        early_ok = True
        for p in V.EARLY:
            d, (lo, hi), n = paired(pool[pool['period'] == p], col, 'r')
            early_ok &= bool(d > 0)
            print(f'  отбор {p}: {n} заявок, разница {d:+.4f} [{lo:+.4f}; {hi:+.4f}]')
        late = pool[V.late_mask(pool)]
        d, (lo, hi), n = paired(late, col, 'r')
        d_ft, _, _ = paired(late, col + '_ft', 'r_ft')
        d_o, (lo_o, hi_o), n_o = paired(other, col, 'r')
        ok = early_ok and lo > 0 and d_ft > 0 and d_o > 0
        print(f'  приёмка: {n} заявок, разница {d:+.4f} [{lo:+.4f}; {hi:+.4f}], насквозь {d_ft:+.4f}')
        print(f'  10 пар вне пула (все периоды): {n_o} заявок, разница {d_o:+.4f} [{lo_o:+.4f}; {hi_o:+.4f}]')
        changed = (f[col] != f['r']).sum()
        print(f'  заявок, где правило сработало: {changed} из {len(f)}; '
              f'{"ПРИНЯТ" if ok else ("отобран, не держится" if early_ok else "не отобран")}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    frame = run()
    frame.to_pickle(os.path.join(OUT, 'r9_orders.pkl'))
    report(frame)
