"""
Зона 1ч → слом 15м → вход на ретесте (PROTOCOL_RETEST.md, записан до расчёта).

    python research/luxsmc/retest.py run      # → results/luxsmc_retest/<пара>.npz (суммы R по дням)
    python research/luxsmc/retest.py dev      # 96 вариантов на DEV + отбор + сделок в месяц
    python research/luxsmc/retest.py val V …  # приёмка на VAL
    LUX_OPEN_HOLD=1 python research/luxsmc/retest.py hold V …
"""
from __future__ import annotations

import argparse
import bisect
import itertools
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
sys.path.insert(0, RESEARCH)
sys.path.insert(0, HERE)

from smcz import data as D                  # noqa: E402
from smcz import sim                        # noqa: E402
from smcz.analyze import boot_ci            # noqa: E402
import lux                                  # noqa: E402
from mtf import levels                      # noqa: E402
from grid import nearest_batch              # noqa: E402

OUT = os.path.join(RESEARCH, 'results', 'luxsmc_retest')
DIMS = {'trend': ('swing', 'internal'), 'poi': ('OB', 'FVG'), 'trig': ('CHoCH', 'any'),
        'entry': ('OB15', 'LVL'), 'stop': ('s15', 's1h'), 'target': ('2R', '3R', 'liq')}
VARIANTS = list(itertools.product(*DIMS.values()))
INDEX = {v: k for k, v in enumerate(VARIANTS)}
POI_LIFE = 72 * 60          # минут
TOUCH_BARS = 16             # свечей 15м от касания до слома
ORDER_MIN = 16 * 15         # заявка на ретест живёт 4 ч
HOLD_MIN = 48 * 60
BUF, MIN_STOP, LIQ_MIN_R = 0.001, 0.003, 1.5
POOL10 = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT',
          'LINKUSDT', 'LTCUSDT')
DAY0 = D.minute('2021-01-01') // 1440
NDAYS = D.minute('2026-10-01') // 1440 - DAY0
EDGES = {'DEV': D.minute('2024-01-01') // 1440 - DAY0, 'VAL': D.minute('2025-04-01') // 1440 - DAY0}


def vname(v):
    return '/'.join(v)


def fvg_zones(B):
    """Имбалансы часа по правилу LuxAlgo как ЗОНЫ: (сторона, низ, верх, бар)."""
    o, h, l, c = B.o, B.h, B.l, B.c
    n = len(c)
    delta = np.full(n, np.nan)
    delta[1:] = (c[:-1] - o[:-1]) / (o[:-1] * 100)
    cum = np.nancumsum(np.abs(np.nan_to_num(delta)))
    out = []
    for i in range(2, n):
        thr = cum[i] / i * 2
        if l[i] > h[i - 2] and c[i - 1] > h[i - 2] and delta[i] > thr:
            out.append((1, h[i - 2], l[i], i))
        if h[i] < l[i - 2] and c[i - 1] < l[i - 2] and -delta[i] > thr:
            out.append((-1, h[i], l[i - 2], i))
    return out


def death_minute(B15, side, bot, top, known):
    """Минута закрытия 15м свечи, ушедшей за дальний край зоны (зона снята)."""
    j = int(np.searchsorted(B15.t, known))
    seg = B15.l[j:] < bot if side > 0 else B15.h[j:] > top
    k = np.flatnonzero(seg)
    return int(B15.t[j + k[0]] + 15) if len(k) else np.iinfo(np.int64).max


def zones(B1, B15, ev1):
    """Зоны 1ч по видам: массивы стороны, низа, верха, известна с, снята с (мин)."""
    out = {}
    ob = [(e.side, e.ob_lo, e.ob_hi, e.i) for e in ev1.itertuples()]
    for name, items in (('OB', ob), ('FVG', fvg_zones(B1))):
        rows = []
        for side, bot, top, i in items:
            if not top > bot:
                continue
            known = int(B1.t[i] + 60)
            rows.append((side, bot, top, known, death_minute(B15, side, bot, top, known)))
        a = np.array(rows, dtype=np.float64).reshape(-1, 5)
        a = a[np.argsort(a[:, 3], kind='stable')]
        out[name] = a
    return out


def trend_at(ev1, B1, scope, minutes):
    x = ev1[ev1.scope == scope]
    if not len(x):
        return np.zeros(len(minutes), np.int64)
    eff = B1.t[x.i.to_numpy()] + 60
    k = np.searchsorted(eff, minutes, side='right') - 1
    return np.where(k >= 0, x.side.to_numpy()[np.clip(k, 0, None)], 0)


def setups(B15, ev15, zs, trend):
    """Сломы 15м после касания живой зоны 1ч по тренду: индексы событий и зона (низ, верх)."""
    ev = ev15[ev15.scope == 'internal']
    ei = ev.i.to_numpy()
    side = ev.side.to_numpy()
    t_close = B15.t[ei] + 15
    known = zs[:, 3]
    out_idx, out_bot, out_top = [], [], []
    for q in range(len(ev)):
        s, j, tc = side[q], ei[q], t_close[q]
        if trend[q] != s:
            continue
        lo = bisect.bisect_left(known, tc - POI_LIFE)
        hi = bisect.bisect_right(known, tc)
        best = None
        for z in range(hi - 1, lo - 1, -1):                      # свежая зона — первой
            zs_, bot, top, kn, dead = zs[z]
            if zs_ != s or dead <= tc:
                continue
            k0 = max(j - TOUCH_BARS, int(np.searchsorted(B15.t, kn)))
            if k0 > j:
                continue
            if s > 0:
                touched = np.any((B15.l[k0:j + 1] <= top) & (B15.l[k0:j + 1] >= bot))
            else:
                touched = np.any((B15.h[k0:j + 1] >= bot) & (B15.h[k0:j + 1] <= top))
            if touched:
                best = (bot, top)
                break
        if best is not None:
            out_idx.append(q)
            out_bot.append(best[0])
            out_top.append(best[1])
    return ev.iloc[out_idx].reset_index(drop=True), np.array(out_bot), np.array(out_top)


def run_pair(pair, only=None):
    t0 = time.time()
    bars = D.load(pair, tfs=('15m', '1h'), src='bybit')
    m1, B15, B1 = bars['base'], bars['15m'], bars['1h']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    piv1 = []
    ev1 = pd.DataFrame(lux.build(B1.h, B1.l, B1.c, pivots=piv1))
    ev15 = pd.DataFrame(lux.build(B15.h, B15.l, B15.c))
    zs_all = zones(B1, B15, ev1)
    liq = levels(B1, B15, piv1)
    acc = np.zeros((len(VARIANTS), NDAYS, 3)) if only is None else None
    trades = []
    internal15 = ev15[ev15.scope == 'internal']
    for tr, pk in itertools.product(DIMS['trend'], DIMS['poi']):
        if only is not None and (tr, pk) != only[:2]:
            continue
        t_close_all = B15.t[internal15.i.to_numpy()] + 15
        trend = trend_at(ev1, B1, tr, t_close_all)
        ev, zbot, ztop = setups(B15, ev15, zs_all[pk], trend)
        if not len(ev):
            continue
        side = ev.side.to_numpy(np.int64)
        long = side > 0
        j = ev.i.to_numpy()
        ref = B15.c[j]
        i_place = B15.i1[j].astype(np.int64)
        t_place = (B15.t[j] + 15).astype(np.float64)
        is_choch = (ev.tag == 'CHoCH').to_numpy()
        for en, sp, tg in itertools.product(DIMS['entry'], DIMS['stop'], DIMS['target']):
            if only is not None and (en, sp, tg) != only[3:]:
                continue
            entry = np.where(long, ev.ob_hi, ev.ob_lo) if en == 'OB15' else ev.level.to_numpy(np.float64)
            entry = np.asarray(entry, np.float64)
            if sp == 's15':
                stop = np.where(long, ev.ob_lo * (1 - BUF), ev.ob_hi * (1 + BUF))
            else:
                stop = np.where(long, zbot * (1 - BUF), ztop * (1 + BUF))
            stop = np.asarray(stop, np.float64)
            risk = (entry - stop) * side
            ok = (risk > 0) & (risk / entry >= MIN_STOP) & ((ref - entry) * side > 0)   # ретест — лимит не за рынком
            if tg == 'liq':
                arr = [(s, liq[s]['L']) for s in (1, -1)]
                target = nearest_batch(t_place, entry, LIQ_MIN_R * np.abs(risk), side,
                                       np.concatenate([a[:, 0] for _, a in arr]),
                                       np.concatenate([a[:, 1] for _, a in arr]),
                                       np.concatenate([a[:, 2] for _, a in arr]),
                                       np.concatenate([np.full(len(a), s, np.int64) for s, a in arr]))
                ok &= ~np.isnan(target)
            else:
                target = entry + side * (2.0 if tg == '2R' else 3.0) * risk
            sel = np.flatnonzero(ok)
            if not len(sel):
                continue
            od = {'side': side[sel], 'i_place': i_place[sel], 'i_expire': i_place[sel] + ORDER_MIN,
                  'entry': entry[sel], 'stop': stop[sel], 'target': target[sel],
                  'hold': np.full(len(sel), HOLD_MIN, np.int64)}
            r = sim.run(m1, od, exclusive=False, cumf=cumf)
            st, end, ip = (r[k].astype(np.int64) for k in ('status', 'end_i', 'i_place'))
            r15 = r['r_gross'] - 1.5 * r['fee_r'] - r['fund_r']
            choch = is_choch[sel][np.argsort(i_place[sel], kind='stable')]
            for trig in DIMS['trig']:
                v = (tr, pk, trig, en, sp, tg)
                if only is not None and v != only:
                    continue
                m = np.flatnonzero(choch) if trig == 'CHoCH' else np.arange(len(st))
                if not len(m):
                    continue
                keep = sim.one_at_a_time(ip[m], end[m], st[m])
                take = m[keep & (st[m] == sim.ST_FILLED)]
                fill = r['fill_i'][take]
                day = m1.t[fill] // 1440 - DAY0
                good = (day >= 0) & (day < NDAYS)
                take, day = take[good], day[good]
                if only is not None:
                    trades.append(pd.DataFrame({'t': m1.t[r['fill_i'][take]], 'r': r['r_net'][take],
                                                'r15': r15[take], 'pair': pair,
                                                'hold_h': (r['end_i'][take] - r['fill_i'][take]) / 60}))
                    continue
                a = acc[INDEX[v]]
                a[:, 0] += np.bincount(day, minlength=NDAYS)
                a[:, 1] += np.bincount(day, r['r_net'][take], minlength=NDAYS)
                a[:, 2] += np.bincount(day, r15[take], minlength=NDAYS)
    if only is not None:
        return pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f'{pair}.npz')
    np.savez_compressed(path[:-4] + '.tmp.npz', acc=acc.astype(np.float32))
    os.replace(path[:-4] + '.tmp.npz', path)
    return pair, f'{time.time() - t0:.0f} с'


# ── Разбор ──────────────────────────────────────────────────────────────────

def load(period, pairs=None):
    lo = 0 if period == 'DEV' else EDGES['DEV']
    hi = EDGES['DEV'] if period == 'DEV' else EDGES['VAL'] if period == 'VAL' else NDAYS
    per = {f[:-4]: np.load(os.path.join(OUT, f))['acc'][:, lo:hi, :].astype(np.float64)
           for f in sorted(os.listdir(OUT)) if f.endswith('.npz') and '.tmp' not in f
           and (pairs is None or f[:-4] in pairs)}
    return sum(per.values()), per, hi - lo


def clustered(a):
    n, s, s15 = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    N = n.sum(1)
    with np.errstate(invalid='ignore', divide='ignore'):
        mu = s.sum(1) / N
        e = s - n * mu[:, None]
        days = (n > 0).sum(1)
        t = mu / np.sqrt((e ** 2).sum(1) / N ** 2 * days / np.maximum(days - 1, 1))
    return pd.DataFrame({'n': N, 'mean': mu, 't_day': t, 'mean15': s15.sum(1) / N},
                        index=[vname(v) for v in VARIANTS])


def dev():
    acc, _, ndays = load('DEV')
    tb = clustered(acc)
    acc10, _, _ = load('DEV', POOL10)
    tb['в_мес_31'] = tb.n / (ndays / 30.4)
    tb['в_мес_10'] = acc10[:, :, 0].sum(1) / (ndays / 30.4)
    tb['R_10пар'] = acc10[:, :, 1].sum(1) / np.maximum(acc10[:, :, 0].sum(1), 1)
    pd.set_option('display.width', 220)
    print('DEV 2021–2023, 31 пара: R на сделку после издержек, t по дням; сделок в месяц — на 31 и на 10 парах SMC')
    print(tb.sort_values('t_day', ascending=False).head(30).round(3).to_string())
    m = tb[tb.n >= 300]
    print(f'\nвариантов с ≥ 300 сделками {len(m)}: в плюсе {(m["mean"] > 0).mean():.0%}, средний R {m["mean"].mean():+.3f}')
    for k, name in enumerate(DIMS):
        g = pd.Series(m['mean'].to_numpy(), index=[v.split('/')[k] for v in m.index]).groupby(level=0).mean()
        print(f'  {name:7s}', ' '.join(f'{i}={x:+.3f}' for i, x in g.items()))
    tb['fee'] = 2 * (tb['mean'] - tb['mean15'])
    print('до издержек (средний по вариантам): %+.3fR; издержки %.3fR' % ((m['mean'] + 2 * (m['mean'] - m['mean15'])).mean(),
                                                                       (2 * (m['mean'] - m['mean15'])).mean()))
    arr = np.array(VARIANTS)
    cands = []
    for k in np.flatnonzero((tb.n >= 300) & (tb['mean'] > 0) & (tb.t_day >= 2.5)):
        nb = np.flatnonzero((arr != arr[k]).sum(1) == 1)
        share = (tb.iloc[nb]['mean'] >= 0).mean() if len(nb) else 0
        cands.append((tb.iloc[k].t_day, tb.index[k], share))
    cands.sort(reverse=True)
    print('\nкандидаты (≥ 300 сделок, R > 0, t по дням ≥ 2.5):', [(v, round(t, 2), f'{sh:.0%}') for t, v, sh in cands] or 'нет')
    print('на VAL идут:', [v for t, v, sh in cands if sh >= 0.5][:3] or 'никто')
    tb.to_csv(os.path.join(OUT, 'dev_table.csv'))


def accept(periods, names):
    for name in names:
        v = tuple(name.split('/'))
        tr = pd.concat([run_pair(p, only=v) for p in D.bybit_pairs()], ignore_index=True)
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
              f'пар в плюсе {share:.0%}, удержание медиана {g.hold_h.median():.1f} ч, годы {years.round(3).to_dict()}')
        print(f'   1 {"да" if c1 else "нет"} · 2 {"да" if c2 else "нет"} · 3 {"да" if c3 else "нет"} · '
              f'4 {"да" if c4 else "нет"} → {"ПРИНЯТ" if c1 and c2 and c3 and c4 else "не принят"}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('run', 'dev', 'val', 'hold'))
    ap.add_argument('variants', nargs='*')
    ap.add_argument('--pairs', nargs='*')
    a = ap.parse_args()
    if a.cmd == 'run':
        for pair in a.pairs or D.bybit_pairs():
            path = os.path.join(OUT, f'{pair}.npz')
            print(pair, 'уже есть' if os.path.exists(path) else run_pair(pair)[1], flush=True)
    elif a.cmd == 'dev':
        dev()
    elif a.cmd == 'val':
        accept(('DEV', 'VAL'), a.variants)
    else:
        if os.getenv('LUX_OPEN_HOLD') != '1':
            raise SystemExit('HOLD закрыт до согласия владельца (LUX_OPEN_HOLD=1)')
        accept(('DEV', 'VAL', 'HOLD'), a.variants)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
