"""SMC с нуля — таблица событий «снятие ликвидности» с признаками и исходами.

python smc0_events.py dev   → research/results/smc0/events_dev.pkl
"""
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smc0_data as D
import smc0_engine as E

OUT = os.path.join(D.BASE, 'results', 'smc0')
os.makedirs(OUT, exist_ok=True)


def features(S, ev):
    t, b0, side = ev['t'], ev['b0'], ev['side']
    a = S.atr[t]
    c = S.c
    f = {}
    f['depth_atr'] = abs(ev['ext'] - ev['level']) / a
    f['reclaim_atr'] = side * (c[t] - ev['level']) / a
    f['sweep_bars'] = t - b0 + 1
    f['atr_pct'] = a / c[t] * 100
    v = S.v[b0:t + 1].sum()
    f['vol_x'] = v / (S.vmed[t] * (t - b0 + 1)) if S.vmed[t] > 0 else np.nan
    # поток тейкеров на снятии: доля покупок против её фона за 48 ч
    br = S.tb[b0:t + 1].sum() / v if v > 0 else np.nan
    vb = S.v[max(0, b0 - 48):b0].sum()
    br0 = S.tb[max(0, b0 - 48):b0].sum() / vb if vb > 0 else np.nan
    f['taker_buy'] = br
    f['taker_buy_rel'] = br - br0
    # по стороне сделки: >0 — агрессия ПРОТИВ сделки на снятии (продавали на лоу)
    f['aggr_against'] = -side * (br - br0)
    # ОИ на снятии и за сутки перед ним
    oi = S.oi
    f['oi_sweep'] = oi[t] / oi[b0 - 1] - 1 if oi[b0 - 1] > 0 else np.nan
    f['oi_24h'] = oi[b0 - 1] / oi[b0 - 25] - 1 if b0 >= 25 and oi[b0 - 25] > 0 else np.nan
    f['oi_after'] = np.nan
    f['fund_bp'] = S.fund[t] * 1e4
    f['fund_side'] = side * S.fund[t] * 1e4  # >0 — толпа против сделки не стоит… см. ниже
    # ход в снятие
    f['mom24'] = side * (c[b0 - 1] - c[max(0, b0 - 25)]) / a
    f['mom168'] = side * (c[t] - c[max(0, t - 168)]) / a
    lo30 = S.l[max(0, t - 720):t + 1].min()
    hi30 = S.h[max(0, t - 720):t + 1].max()
    f['pos30'] = (c[t] - lo30) / (hi30 - lo30) if hi30 > lo30 else np.nan
    lo7 = S.l[max(0, t - 168):t + 1].min()
    hi7 = S.h[max(0, t - 168):t + 1].max()
    f['pos7'] = (c[t] - lo7) / (hi7 - lo7) if hi7 > lo7 else np.nan
    f['age'] = ev['age']
    f['n_pools'] = ev['n_pools']
    f['hour'] = S.hour[t]
    f['dow'] = S.idx[t].dayofweek
    return f


def outcomes(S, ev):
    t, side = ev['t'], ev['side']
    a = S.atr[t]
    c = S.c
    out = {}
    for hz in (6, 12, 24, 48):
        j = min(S.n - 1, t + hz)
        out[f'fwd{hz}'] = side * (c[j] - c[t]) / a
    stop = ev['ext'] - side * 0.1 * a
    # рынок на закрытии, цель 2R
    ent = c[t]
    for rr in (1.5, 2.0, 3.0):
        tgt = ent + side * rr * abs(ent - stop)
        r = E.simulate(S, side, t, ent, stop, tgt, 1, 48, market=True)
        out[f'mkt_{rr}'] = r['R'] if r and r.get('filled') else np.nan
    # лимит на 50% отката к экстремуму
    ent = (c[t] + ev['ext']) / 2
    for rr in (2.0, 3.0):
        tgt = ent + side * rr * abs(ent - stop)
        r = E.simulate(S, side, t, ent, stop, tgt, 12, 48)
        out[f'lim50_{rr}'] = r['R'] if r and r.get('filled') else np.nan
    out['stop_pct'] = abs(c[t] - stop) / c[t] * 100
    return out


def build(period, syms=None, folders=None, **kw):
    rows = []
    syms = syms or D.POOL
    for sym in syms:
        df = D.load(sym, period, folders)
        if df is None:
            continue
        S = E.Series(df)
        evs = E.liquidity_events(S, **kw)
        for ev in evs:
            t = ev['t']
            if not S.sig_ok[t] or np.isnan(S.atr[t]) or ev['b0'] < 30:
                continue
            if S.gap[max(0, t - 24):t + 1].any():
                continue
            r = {'sym': sym, 'ts': S.idx[t], 'side': ev['side']}
            r.update(features(S, ev))
            r.update(outcomes(S, ev))
            rows.append(r)
    return pd.DataFrame(rows)


if __name__ == '__main__':
    per = sys.argv[1] if len(sys.argv) > 1 else 'dev'
    ev = build(per)
    ev.to_pickle(os.path.join(OUT, f'events_{per}.pkl'))
    print(per, len(ev), 'событий;', f"{len(ev) / ((ev.ts.max() - ev.ts.min()).days / 7):.1f} в неделю")
