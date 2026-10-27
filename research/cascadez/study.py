"""
Ликвидационные каскады (PROTOCOL.md, записан до расчёта): резкий ход 5/15 мин
и падение открытого интереса Binance → против каскада или по нему.

    python research/cascadez/study.py run [--workers 2]   # → results/cascadez/<пара>.npz (суммы по дням)
    python research/cascadez/study.py dev                 # 108 вариантов на DEV + отбор
    python research/cascadez/study.py val V ...           # приёмка на VAL (V = W/z/oi/mode/h)
    CASCADE_OPEN_HOLD=1 python research/cascadez/study.py hold V ...
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
sys.path.insert(0, RESEARCH)

from smcz import data as D                  # noqa: E402
from smcz import sim                        # noqa: E402
from smcz.analyze import boot_ci            # noqa: E402

OUT = os.path.join(RESEARCH, 'results', 'cascadez')
METRICS = os.path.join(RESEARCH, 'binance_metrics_cache')
WS, ZS, OIS = (5, 15), (3.0, 5.0), ('any', 'oi0.5', 'oi1.5')
MODES, HS = ('fade_time', 'fade_half', 'cont_time'), (15, 60, 240)
MIN_MOVE, BUF, FAR = 0.0075, 0.001, 1000.0
VARIANTS = list(itertools.product(WS, ZS, OIS, MODES, HS))
INDEX = {v: k for k, v in enumerate(VARIANTS)}
DAY0 = D.minute('2021-01-01') // 1440
NDAYS = D.minute('2026-10-01') // 1440 - DAY0
EDGES = {'DEV': D.minute('2024-01-01') // 1440 - DAY0, 'VAL': D.minute('2025-04-01') // 1440 - DAY0}


def vname(v):
    w, z, oi, mode, h = v
    return f'W{w}/z{z:g}/{oi}/{mode}/h{h}'


def pairs():
    have = {f[:-4] for f in os.listdir(METRICS) if f.endswith('.pkl')}
    return [p for p in D.bybit_pairs() if p in have]


def open_interest(pair, minutes):
    """ОИ Binance (контракты) на минуты minutes; nan — снимка нет."""
    m = pd.read_pickle(os.path.join(METRICS, f'{pair}.pkl'))['sum_open_interest'].dropna()
    t = (m.index.as_unit('s').asi8 // 60).astype(np.int64)
    v = m.to_numpy(np.float64)
    k = np.searchsorted(t, minutes)
    ok = (k < len(t)) & (t[np.minimum(k, len(t) - 1)] == minutes)
    return np.where(ok, v[np.minimum(k, len(t) - 1)], np.nan)


def cascades(B, oi_close, w):
    """Свечи-каскады окна w свечей 5м: индексы, z, сторона, экстремум, цена до, ΔOI."""
    c, h, l = B.c, B.h, B.l
    n = len(c)
    r = np.full(n, np.nan)
    r[w:] = np.log(c[w:] / c[:-w])
    scale = pd.Series(r).shift(1).rolling(288, min_periods=200).std().to_numpy()
    lo = pd.Series(l).rolling(w).min().to_numpy()
    hi = pd.Series(h).rolling(w).max().to_numpy()
    doi = np.full(n, np.nan)
    doi[w:] = oi_close[w:] / oi_close[:-w] - 1
    with np.errstate(invalid='ignore'):
        base = (np.abs(r) >= np.maximum(ZS[0] * scale, MIN_MOVE)) & (scale > 0)
    idx = np.flatnonzero(base)
    idx = idx[idx >= w]
    side = np.sign(r[idx]).astype(np.int64)
    extreme = np.where(side < 0, lo[idx], hi[idx])
    return idx, np.abs(r[idx]) / scale[idx], side, extreme, c[idx - w], doi[idx]


def run_pair(pair, only=None):
    t0 = time.time()
    bars = D.load(pair, tfs=('5m',), src='bybit')
    m1, B = bars['base'], bars['5m']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    oi_close = open_interest(pair, B.t + B.tf)
    acc = np.zeros((len(VARIANTS), NDAYS, 3), np.float64) if only is None else None
    trades = []
    for w_min in WS:
        if only is not None and only[0] != w_min:
            continue
        idx, zval, cside, extreme, start, doi = cascades(B, oi_close, w_min // 5)
        if not len(idx):
            continue
        ref = B.c[idx]
        for mode, hold in itertools.product(MODES, HS):
            if only is not None and (mode, hold) != (only[3], only[4]):
                continue
            if mode.startswith('fade'):
                s = -cside
                stop = np.where(cside < 0, extreme * (1 - BUF), extreme * (1 + BUF))
            else:
                s = cside
                stop = start
            if mode == 'fade_half':
                target = (start + extreme) / 2
            else:
                target = ref + s * FAR * np.abs(ref - stop)
            od = {'side': s, 'i_place': B.i1[idx].astype(np.int64), 'i_expire': B.i1[idx].astype(np.int64) + 1,
                  'entry': np.full(len(idx), np.nan), 'stop': stop, 'target': target,
                  'hold': np.full(len(idx), hold, np.int64)}
            r = sim.run(m1, od, exclusive=False, cumf=cumf)
            st, end, ip = (r[k].astype(np.int64) for k in ('status', 'end_i', 'i_place'))
            r15 = r['r_gross'] - 1.5 * r['fee_r'] - r['fund_r']
            day = np.minimum(ip, len(m1.t) - 1)
            day = m1.t[day] // 1440 - DAY0
            for z, oi in itertools.product(ZS, OIS):
                v = (w_min, z, oi, mode, hold)
                if only is not None and v != only:
                    continue
                msk = zval >= z
                if oi != 'any':
                    with np.errstate(invalid='ignore'):
                        msk &= doi <= -float(oi[2:]) / 100
                sel = np.flatnonzero(msk)
                if not len(sel):
                    continue
                keep = sim.one_at_a_time(ip[sel], end[sel], st[sel])
                take = sel[keep & (st[sel] == sim.ST_FILLED)]
                take = take[(day[take] >= 0) & (day[take] < NDAYS)]
                if only is not None:
                    trades.append(pd.DataFrame({'t': m1.t[ip[take]], 'r': r['r_net'][take],
                                                'r15': r15[take], 'pair': pair}))
                    continue
                a = acc[INDEX[v]]
                a[:, 0] += np.bincount(day[take], minlength=NDAYS)
                a[:, 1] += np.bincount(day[take], r['r_net'][take], minlength=NDAYS)
                a[:, 2] += np.bincount(day[take], r15[take], minlength=NDAYS)
    if only is not None:
        return pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f'{pair}.npz')
    np.savez_compressed(path[:-4] + '.tmp.npz', acc=acc.astype(np.float32))
    os.replace(path[:-4] + '.tmp.npz', path)
    return pair, f'{time.time() - t0:.0f} с'


def _run(pair):
    if os.path.exists(os.path.join(OUT, f'{pair}.npz')):
        return pair, 'уже есть'
    return run_pair(pair)


# ── Разбор ──────────────────────────────────────────────────────────────────

def load(period):
    """Суммы по дням периода: всё вместе и по парам."""
    lo = 0 if period == 'DEV' else EDGES['DEV']
    hi = EDGES['DEV'] if period == 'DEV' else EDGES['VAL'] if period == 'VAL' else NDAYS
    per = {}
    for f in sorted(os.listdir(OUT)):
        if f.endswith('.npz') and '.tmp' not in f:
            per[f[:-4]] = np.load(os.path.join(OUT, f))['acc'][:, lo:hi, :].astype(np.float64)
    return sum(per.values()), per


def clustered(a):
    """Средний R, t по дням (сумма R дня — единица), R при ×1.5, сделок."""
    n, s, s15 = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    N = n.sum(1)
    with np.errstate(invalid='ignore', divide='ignore'):
        mu = s.sum(1) / N
        e = s - n * mu[:, None]
        days = (n > 0).sum(1)
        var = (e ** 2).sum(1) / N ** 2 * days / np.maximum(days - 1, 1)
        t = mu / np.sqrt(var)
    return pd.DataFrame({'n': N, 'mean': mu, 't_day': t, 'mean15': s15.sum(1) / N, 'days': days},
                        index=[vname(v) for v in VARIANTS])


def dev():
    acc, _ = load('DEV')
    tb = clustered(acc)
    pd.set_option('display.width', 200)
    print('DEV (до 2023-12, ОИ у большинства пар с 2021-12): R на сделку после издержек, t по дням')
    print(tb[tb.n >= 300].sort_values('t_day', ascending=False).head(25).round(3).to_string())
    m = tb[tb.n >= 300]
    print(f'\nвариантов с ≥ 300 сделками {len(m)}: в плюсе {(m["mean"] > 0).mean():.0%}, средний R {m["mean"].mean():+.3f}')
    for k, name in enumerate(('W', 'z', 'ОИ', 'выход', 'h')):
        g = pd.Series(m['mean'].to_numpy(), index=[v.split('/')[k] for v in m.index]).groupby(level=0).mean()
        print(f'  {name:6s}', ' '.join(f'{i}={x:+.3f}' for i, x in g.items()))
    arr = np.array([[str(x) for x in v] for v in VARIANTS])
    cands = []
    for k in np.flatnonzero((tb.n >= 300) & (tb['mean'] > 0) & (tb.t_day >= 2.5)):
        nb = np.flatnonzero((arr != arr[k]).sum(1) == 1)
        nb = nb[tb.iloc[nb].n.to_numpy() > 0]
        share = (tb.iloc[nb]['mean'] >= 0).mean() if len(nb) else 0
        cands.append((tb.iloc[k].t_day, tb.index[k], share))
    cands.sort(reverse=True)
    print('\nкандидаты (≥ 300 сделок, R > 0, t по дням ≥ 2.5):')
    for t, v, sh in cands[:10]:
        print(f'  {v:34s} t={t:.2f} соседей не в минусе {sh:.0%}')
    chosen = [v for t, v, sh in cands if sh >= 0.5][:3]
    print('на VAL идут:', chosen or 'никто')
    tb.to_csv(os.path.join(OUT, 'dev_table.csv'))


def accept(periods, names):
    for name in names:
        w, z, oi, mode, h = name.split('/')
        v = (int(w[1:]), float(z[1:]), oi, mode, int(h[1:]))
        tr = pd.concat([run_pair(p, only=v) for p in pairs()], ignore_index=True)
        day = tr.t // 1440 - DAY0
        tr['period'] = np.where(day < EDGES['DEV'], 'DEV', np.where(day < EDGES['VAL'], 'VAL', 'HOLD'))
        tr = tr[tr.period.isin(periods)]
        tr['year'] = pd.to_datetime(tr.t * 60, unit='s', utc=True).dt.year
        g = tr[tr.period == periods[-1]]
        r = g.r.to_numpy()
        lo, hi = boot_ci(g.t.to_numpy(), r)
        pr = g.groupby('pair').r.agg(['size', 'mean'])
        pr = pr[pr['size'] >= 10]
        share = (pr['mean'] > 0).mean() if len(pr) else np.nan
        years = tr.groupby('year').r.mean()
        tt = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 else np.nan
        c1 = len(r) > 0 and r.mean() > 0 and (lo > 0 or (len(r) >= 300 and tt > 2 and lo >= 0))
        c2, c3, c4 = g.r15.mean() > 0, share >= 0.55, bool((years >= -0.05).all())
        print(f'{name} на {periods[-1]}: n={len(r)} {r.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}] ×1.5 {g.r15.mean():+.3f}, '
              f'пар в плюсе {share:.0%}, годы {years.round(3).to_dict()}')
        print(f'   1 {"да" if c1 else "нет"} · 2 {"да" if c2 else "нет"} · 3 {"да" if c3 else "нет"} · '
              f'4 {"да" if c4 else "нет"} → {"ПРИНЯТ" if c1 and c2 and c3 and c4 else "не принят"}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('run', 'dev', 'val', 'hold'))
    ap.add_argument('variants', nargs='*')
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--pairs', nargs='*')
    a = ap.parse_args()
    if a.cmd == 'run':
        with Pool(a.workers) as pool:
            for pair, msg in pool.imap_unordered(_run, a.pairs or pairs()):
                print(pair, msg, flush=True)
    elif a.cmd == 'dev':
        dev()
    elif a.cmd == 'val':
        accept(('DEV', 'VAL'), a.variants)
    else:
        if os.getenv('CASCADE_OPEN_HOLD') != '1':
            raise SystemExit('HOLD закрыт до согласия владельца (CASCADE_OPEN_HOLD=1)')
        accept(('DEV', 'VAL', 'HOLD'), a.variants)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
