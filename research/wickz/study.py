"""
Ловля тени: лимитные заявки далеко от цены (PROTOCOL.md, записан до расчёта).

    python research/wickz/study.py run       # → results/wickz/<пара>.npz (суммы R по дням)
    python research/wickz/study.py dev       # 30 вариантов на DEV + отбор
    python research/wickz/study.py val V ... # приёмка на VAL (V = k3/s1/h60 …)
    WICK_OPEN_HOLD=1 python research/wickz/study.py hold V ...
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import time

import numpy as np
import pandas as pd
from numba import njit

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
sys.path.insert(0, RESEARCH)

from smcz import data as D                  # noqa: E402
from smcz import sim                        # noqa: E402
from smcz.analyze import boot_ci            # noqa: E402

OUT = os.path.join(RESEARCH, 'results', 'wickz')
KS, SS, EXITS = (3.0, 5.0, 8.0), (1.0, 2.0), ('h15', 'h60', 'h240', 'half', 'full')
FLOOR = 0.005
MAKER, TAKER, SLIP_STOP, SLIP_MKT = 0.0002, 0.00055, 0.0005, 0.0002
VARIANTS = list(itertools.product(KS, SS, EXITS))
INDEX = {v: n for n, v in enumerate(VARIANTS)}
DAY0 = D.minute('2021-01-01') // 1440
NDAYS = D.minute('2026-10-01') // 1440 - DAY0
EDGES = {'DEV': D.minute('2024-01-01') // 1440 - DAY0, 'VAL': D.minute('2025-04-01') // 1440 - DAY0}


def vname(v):
    k, s, ex = v
    return f'k{k:g}/s{s:g}/{ex}'


@njit(cache=True)
def run_orders(o, h, l, c, side, i0, lim, stop, target, hold, cumf):
    """Налив насквозь в течение 5 минут; дальше стоп раньше цели, выход по времени.
    Возвращает: налита ли, минута налива, минута выхода, R до издержек, издержки R, фандинг R."""
    n, N = len(side), len(o)
    filled = np.zeros(n, np.int64)
    fi = np.full(n, -1, np.int64)
    ei = np.full(n, -1, np.int64)
    g = np.full(n, np.nan)
    fee_r = np.full(n, np.nan)
    fund_r = np.full(n, np.nan)
    for q in range(n):
        s, e, sp, tg = side[q], lim[q], stop[q], target[q]
        f = -1
        for i in range(i0[q], min(i0[q] + 5, N)):
            if (s > 0 and l[i] < e) or (s < 0 and h[i] > e):
                f = i
                break
        if f < 0:
            ei[q] = min(i0[q] + 5, N) - 1
            continue
        filled[q] = 1
        fi[q] = f
        risk = abs(e - sp)
        kind = 0
        xi = -1
        xpx = np.nan
        if (s > 0 and l[f] <= sp) or (s < 0 and h[f] >= sp):
            xi, xpx, kind = f, sp, 1
        else:
            last = min(f + hold[q], N - 1)
            for i in range(f + 1, last + 1):
                if s > 0:
                    if l[i] <= sp:
                        xi, xpx, kind = i, min(sp, o[i]), 1
                        break
                    if tg == tg and h[i] > tg:
                        xi, xpx, kind = i, (o[i] if o[i] > tg else tg), 2
                        break
                else:
                    if h[i] >= sp:
                        xi, xpx, kind = i, max(sp, o[i]), 1
                        break
                    if tg == tg and l[i] < tg:
                        xi, xpx, kind = i, (o[i] if o[i] < tg else tg), 2
                        break
            if xi < 0:
                xi, xpx, kind = last, c[last], 3
        ei[q] = xi
        fee_out = TAKER + SLIP_STOP if kind == 1 else (MAKER if kind == 2 else TAKER + SLIP_MKT)
        g[q] = (xpx - e) * s / risk
        fee_r[q] = (MAKER * e + fee_out * xpx) / risk
        fund_r[q] = s * (cumf[xi] - cumf[f]) * e / risk
    return filled, fi, ei, g, fee_r, fund_r


def candidates(B, k):
    """Заявки, которые нальются (следующая свеча прошла лимит насквозь):
    сторона, минута постановки, лимит, цена постановки, отступ (доля)."""
    c = B.c
    r = np.full(len(c), np.nan)
    r[1:] = np.log(c[1:] / c[:-1])
    sig = pd.Series(r).rolling(288, min_periods=200).std().to_numpy()
    d = np.maximum(k * sig, FLOOR)
    i = np.arange(len(c) - 1)
    ok = ~np.isnan(sig[i]) & (B.i1[i] == B.i0[i + 1])                    # следующая свеча — сразу
    buy = ok & (B.l[i + 1] < c[i] * (1 - d[i]))
    sell = ok & (B.h[i + 1] > c[i] * (1 + d[i]))
    rows = []
    for mask, s in ((buy, 1), (sell, -1)):
        j = i[mask]
        rows.append((np.full(len(j), s, np.int64), B.i1[j].astype(np.int64), c[j] * (1 - s * d[j]), c[j], d[j]))
    side, ip, lim, ref, dd = (np.concatenate(x) for x in zip(*rows))
    order = np.argsort(ip, kind='stable')
    return side[order], ip[order], lim[order], ref[order], dd[order]


def run_pair(pair, only=None):
    t0 = time.time()
    bars = D.load(pair, tfs=('5m',), src='bybit')
    m1, B = bars['base'], bars['5m']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    if cumf is None:
        cumf = np.zeros(len(m1.t))
    acc = np.zeros((len(VARIANTS), NDAYS, 3)) if only is None else None
    trades = []
    for k in KS:
        if only is not None and only[0] != k:
            continue
        side, ip, lim, ref, d = candidates(B, k)
        if not len(side):
            continue
        for s_mult, ex in itertools.product(SS, EXITS):
            v = (k, s_mult, ex)
            if only is not None and v != only:
                continue
            stop = lim - side * s_mult * d * ref
            if ex == 'half':
                target, hold = lim + side * 0.5 * d * ref, 240
            elif ex == 'full':
                target, hold = ref.copy(), 240
            else:
                target, hold = np.full(len(side), np.nan), int(ex[1:])
            filled, fi, ei, g, fee, fund = run_orders(m1.o, m1.h, m1.l, m1.c, side, ip, lim, stop, target,
                                                      np.full(len(side), hold, np.int64), cumf)
            status = np.where(filled == 1, sim.ST_FILLED, sim.ST_EXPIRED).astype(np.int64)
            keep = sim.one_at_a_time(ip, ei, status)
            take = np.flatnonzero(keep & (filled == 1))
            rn, r15 = g - fee - fund, g - 1.5 * fee - fund
            day = m1.t[fi[take]] // 1440 - DAY0
            good = (day >= 0) & (day < NDAYS)
            take, day = take[good], day[good]
            if only is not None:
                trades.append(pd.DataFrame({'t': m1.t[fi[take]], 'r': rn[take], 'r15': r15[take],
                                            'side': side[take], 'pair': pair}))
                continue
            a = acc[INDEX[v]]
            a[:, 0] += np.bincount(day, minlength=NDAYS)
            a[:, 1] += np.bincount(day, rn[take], minlength=NDAYS)
            a[:, 2] += np.bincount(day, r15[take], minlength=NDAYS)
    if only is not None:
        return pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f'{pair}.npz')
    np.savez_compressed(path[:-4] + '.tmp.npz', acc=acc.astype(np.float32))
    os.replace(path[:-4] + '.tmp.npz', path)
    return pair, f'{time.time() - t0:.0f} с'


# ── Разбор (как research/cascadez) ──────────────────────────────────────────

def load(period):
    lo = 0 if period == 'DEV' else EDGES['DEV']
    hi = EDGES['DEV'] if period == 'DEV' else EDGES['VAL'] if period == 'VAL' else NDAYS
    per = {f[:-4]: np.load(os.path.join(OUT, f))['acc'][:, lo:hi, :].astype(np.float64)
           for f in sorted(os.listdir(OUT)) if f.endswith('.npz') and '.tmp' not in f}
    return sum(per.values()), per


def clustered(a):
    n, s, s15 = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    N = n.sum(1)
    with np.errstate(invalid='ignore', divide='ignore'):
        mu = s.sum(1) / N
        e = s - n * mu[:, None]
        days = (n > 0).sum(1)
        t = mu / np.sqrt((e ** 2).sum(1) / N ** 2 * days / np.maximum(days - 1, 1))
    return pd.DataFrame({'n': N, 'mean': mu, 't_day': t, 'mean15': s15.sum(1) / N, 'days': days},
                        index=[vname(v) for v in VARIANTS])


def dev():
    acc, _ = load('DEV')
    tb = clustered(acc)
    tb['fee'] = 2 * (tb['mean'] - tb['mean15'])
    tb['gross'] = tb['mean'] + tb['fee']
    pd.set_option('display.width', 200)
    print('DEV 2021–2023: R на сделку после издержек, t по дням; gross — до комиссий и проскальзывания')
    print(tb.sort_values('t_day', ascending=False).round(3).to_string())
    arr = np.array([[str(x) for x in v] for v in VARIANTS])
    cands = []
    for k in np.flatnonzero((tb.n >= 300) & (tb['mean'] > 0) & (tb.t_day >= 2.5)):
        nb = np.flatnonzero((arr != arr[k]).sum(1) == 1)
        share = (tb.iloc[nb]['mean'] >= 0).mean() if len(nb) else 0
        cands.append((tb.iloc[k].t_day, tb.index[k], share))
    cands.sort(reverse=True)
    print('\nкандидаты (≥ 300 сделок, R > 0, t по дням ≥ 2.5):', [(v, round(t, 2), f'{sh:.0%}') for t, v, sh in cands] or 'нет')
    chosen = [v for t, v, sh in cands if sh >= 0.5][:3]
    print('на VAL идут:', chosen or 'никто')
    tb.to_csv(os.path.join(OUT, 'dev_table.csv'))


def pairs():
    return D.bybit_pairs()


def accept(periods, names):
    for name in names:
        k, s, ex = name.split('/')
        v = (float(k[1:]), float(s[1:]), ex)
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
    ap.add_argument('--pairs', nargs='*')
    a = ap.parse_args()
    if a.cmd == 'run':
        for pair in a.pairs or pairs():
            path = os.path.join(OUT, f'{pair}.npz')
            print(pair, 'уже есть' if os.path.exists(path) else run_pair(pair)[1], flush=True)
    elif a.cmd == 'dev':
        dev()
    elif a.cmd == 'val':
        accept(('DEV', 'VAL'), a.variants)
    else:
        if os.getenv('WICK_OPEN_HOLD') != '1':
            raise SystemExit('HOLD закрыт до согласия владельца (WICK_OPEN_HOLD=1)')
        accept(('DEV', 'VAL', 'HOLD'), a.variants)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
