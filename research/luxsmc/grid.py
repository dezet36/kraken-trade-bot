"""
Широкий перебор правил по разметке LuxAlgo (PROTOCOL_GRID.md, записан до
расчёта): 7 200 вариантов, 31 пара Bybit.

Исполнение считается один раз на набор заявок (ТФ входа, блок, вход в блок,
цель, ТФ уровней, срок), фильтры — направление, слом, premium/discount —
накладываются масками; «одна заявка на пару» — после маски. На диск по паре
идут только суммы по (период, год) — разбор видит HOLD лишь с LUX_OPEN_HOLD=1.

    python research/luxsmc/grid.py run [--workers 2]   # → results/luxsmc_grid/<пара>.npz
    python research/luxsmc/grid.py dev                  # отбор на DEV
    python research/luxsmc/grid.py val <вариант> ...    # приёмка на VAL
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
from numba import njit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from smcz import data as D                  # noqa: E402
from smcz import sim                        # noqa: E402
from smcz.analyze import boot_ci            # noqa: E402
import lux                                  # noqa: E402
from mtf import bias_at, levels             # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), 'results', 'luxsmc_grid')
HIGHER = {'15m': '1h', '1h': '4h', '4h': '1d'}
DIMS = {
    'E': ('15m', '1h', '4h'),
    'Lv': ('same', 'high'),
    'dir': ('none', 'Es', 'Ei', 'Ls', 'Li'),
    'ob': ('internal', 'swing'),
    'brk': ('any', 'BOS', 'CHoCH'),
    'entry': ('near', 'mid'),
    'target': ('L', 'F', 'LF', '2R', '3R'),
    'pd': ('off', 'on'),
    'exp': (24, 72),
}
NAMES = tuple(DIMS)
VARIANTS = list(itertools.product(*DIMS.values()))
INDEX = {v: k for k, v in enumerate(VARIANTS)}
STOP_BUF, MIN_STOP, MIN_R = 0.001, 0.0025, 1.0
HOLD_MIN = {'15m': 7 * 1440, '1h': 7 * 1440, '4h': 30 * 1440}
YEARS = tuple(range(2021, 2027))
PERIODS = ('DEV', 'VAL', 'HOLD')
NB = len(YEARS) * len(PERIODS)            # ячейки (год, период)
STATS = ('n', 'sum', 'sumsq', 'sum15', 'win')


def vname(v):
    return '/'.join(str(x) for x in v)


@njit(cache=True)
def nearest_batch(t, entry, mind, side, lv, kn, gn, cs):
    """Ближайший живой уровень в сторону сделки не ближе mind от входа."""
    n, m = len(t), len(lv)
    out = np.full(n, np.nan)
    for q in range(n):
        best = np.inf
        for k in range(m):
            if cs[k] != side[q] or kn[k] > t[q] or gn[k] <= t[q]:
                continue
            d = (lv[k] - entry[q]) * side[q]
            if d >= mind[q] and d < best:
                best = d
                out[q] = lv[k]
    return out


def cells(t_min):
    """Номер ячейки (год, период) по минуте постановки."""
    year = (t_min * 60).astype('datetime64[s]').astype('datetime64[Y]').astype(np.int64) + 1970
    per = D.period_of(t_min)
    pc = np.select([per == 'DEV', per == 'VAL', per == 'HOLD'], [0, 1, 2], -1)
    yi = np.clip(year - YEARS[0], 0, len(YEARS) - 1)
    return np.where(pc >= 0, yi * len(PERIODS) + pc, -1)


def run_pair(pair, only=None):
    """Суммы по всем вариантам; only — один вариант: вернуть его сделки."""
    t0 = time.time()
    bars = D.load(pair, tfs=('15m', '1h', '4h', '1d'), src='bybit')
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    tfs = ('15m', '1h', '4h', '1d') if only is None else tuple({only[0], HIGHER[only[0]]})
    ev, lvl = {}, {}
    for tf in tfs:
        piv = []
        ev[tf] = pd.DataFrame(lux.build(bars[tf].h, bars[tf].l, bars[tf].c, pivots=piv))
        lvl[tf] = levels(bars[tf], bars['15m'], piv)
    acc = np.zeros((len(VARIANTS), NB, len(STATS))) if only is None else None
    trades = []
    for E in DIMS['E']:
        if only is not None and only[0] != E:
            continue
        BE = bars[E]
        lv_tf = {'same': E, 'high': HIGHER[E]}
        for ob in DIMS['ob']:
            x = ev[E][ev[E].scope == ob] if len(ev[E]) else ev[E]
            if not len(x):
                continue
            side = x.side.to_numpy(np.int64)
            long = side == 1
            bi = x.i.to_numpy(np.int64)
            t_place = BE.t[bi] + BE.tf
            i_place = BE.i1[bi].astype(np.int64)
            hi, lo = x.ob_hi.to_numpy(np.float64), x.ob_lo.to_numpy(np.float64)
            stop = np.where(long, lo * (1 - STOP_BUF), hi * (1 + STOP_BUF))
            dirs = {'none': np.ones(len(x), bool),
                    'Es': x.swing_bias.to_numpy() == side, 'Ei': x.internal_bias.to_numpy() == side}
            for lk, tf in lv_tf.items():
                if tf in ev:
                    dirs[('Ls', lk)] = bias_at(ev[tf], 'swing', t_place, bars[tf]) == side
                    dirs[('Li', lk)] = bias_at(ev[tf], 'internal', t_place, bars[tf]) == side
            brks = {'any': np.ones(len(x), bool), 'BOS': (x.tag == 'BOS').to_numpy(),
                    'CHoCH': (x.tag == 'CHoCH').to_numpy()}
            mid_tr = ((x.trail_top + x.trail_bottom) / 2).to_numpy(np.float64)
            for em in DIMS['entry']:
                entry = np.where(long, hi, lo) if em == 'near' else (hi + lo) / 2
                risk = np.abs(entry - stop)
                ok = risk / entry >= MIN_STOP
                with np.errstate(invalid='ignore'):
                    pds = {'off': np.ones(len(x), bool),
                           'on': ~np.isnan(mid_tr) & np.where(long, entry < mid_tr, entry > mid_tr)}
                for tg in DIMS['target']:
                    lv_keys = ('same', 'high') if tg in ('L', 'F', 'LF') else ('*',)
                    for lvk in lv_keys:
                        if tg in ('2R', '3R'):
                            target = entry + side * (2.0 if tg == '2R' else 3.0) * risk
                        else:
                            tf = lv_tf[lvk]
                            if tf not in lvl:
                                continue
                            arr = [(s, lvl[tf][s][tg]) for s in (1, -1)]
                            lv_ = np.concatenate([a[:, 0] for _, a in arr])
                            kn_ = np.concatenate([a[:, 1] for _, a in arr])
                            gn_ = np.concatenate([a[:, 2] for _, a in arr])
                            cs_ = np.concatenate([np.full(len(a), s, np.int64) for s, a in arr])
                            target = nearest_batch(t_place.astype(np.float64), entry, MIN_R * risk, side,
                                                   lv_, kn_, gn_, cs_)
                        valid = np.flatnonzero(ok & ~np.isnan(target))
                        if not len(valid):
                            continue
                        for ex in DIMS['exp']:
                            if only is not None and (ob, em, tg, ex) != (only[3], only[5], only[6], only[8]):
                                continue
                            if only is not None and lvk not in ('*', only[1]):
                                continue
                            od = {'side': side[valid], 'i_place': i_place[valid],
                                  'i_expire': i_place[valid] + ex * BE.tf, 'entry': entry[valid],
                                  'stop': stop[valid], 'target': target[valid],
                                  'hold': np.full(len(valid), HOLD_MIN[E], np.int64)}
                            order = np.argsort(od['i_place'], kind='stable')
                            sub = valid[order]
                            r = sim.run(m1, od, exclusive=False, cumf=cumf)
                            st = r['status'].astype(np.int64)
                            end = r['end_i'].astype(np.int64)
                            ip = r['i_place'].astype(np.int64)
                            r15 = r['r_gross'] - 1.5 * r['fee_r'] - r['fund_r']
                            cell = cells(m1.t[np.minimum(ip, len(m1.t) - 1)])
                            for lk in (('same', 'high') if lvk == '*' else (lvk,)):
                                for dk, bk, pk in itertools.product(DIMS['dir'], DIMS['brk'], DIMS['pd']):
                                    dkey = dk if dk in ('none', 'Es', 'Ei') else (dk, lk)
                                    if dkey not in dirs:
                                        continue
                                    v = (E, lk, dk, ob, bk, em, tg, pk, ex)
                                    if only is not None and v != only:
                                        continue
                                    msk = (dirs[dkey] & brks[bk] & pds[pk])[sub]
                                    idx = np.flatnonzero(msk)
                                    if not len(idx):
                                        continue
                                    keep = sim.one_at_a_time(ip[idx], end[idx], st[idx])
                                    take = idx[keep & (st[idx] == sim.ST_FILLED)]
                                    if not len(take):
                                        continue
                                    if only is not None:
                                        trades.append(pd.DataFrame({
                                            't': m1.t[ip[take]], 'r_net': r['r_net'][take],
                                            'r_net15': r15[take], 'side': r['side'][take], 'pair': pair}))
                                        continue
                                    c, rr, r5 = cell[take], r['r_net'][take], r15[take]
                                    good = c >= 0
                                    c, rr, r5 = c[good], rr[good], r5[good]
                                    a = acc[INDEX[v]]
                                    a[:, 0] += np.bincount(c, minlength=NB)
                                    a[:, 1] += np.bincount(c, rr, minlength=NB)
                                    a[:, 2] += np.bincount(c, rr * rr, minlength=NB)
                                    a[:, 3] += np.bincount(c, r5, minlength=NB)
                                    a[:, 4] += np.bincount(c, (rr > 0).astype(np.float64), minlength=NB)
    if only is not None:
        return pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f'{pair}.npz')
    tmp = path[:-4] + '.tmp.npz'
    np.savez_compressed(tmp, acc=acc.astype(np.float64))
    os.replace(tmp, path)
    return pair, f'{time.time() - t0:.0f} с'


def _run(pair):
    if os.path.exists(os.path.join(OUT, f'{pair}.npz')):
        return pair, 'уже есть'
    return run_pair(pair)


# ── Разбор ──────────────────────────────────────────────────────────────────

def load_acc():
    """Суммы по вариантам: всё вместе и по парам."""
    files = sorted(f for f in os.listdir(OUT) if f.endswith('.npz') and '.tmp' not in f)
    per_pair = {f[:-4]: np.load(os.path.join(OUT, f))['acc'] for f in files}
    return sum(per_pair.values()), per_pair


def summary(acc, period):
    """Сделки, средний R, t, R при ×1.5 по варианту за период."""
    pc = PERIODS.index(period)
    sel = [k * len(PERIODS) + pc for k in range(len(YEARS))]
    a = acc[:, sel, :].sum(1)
    n, s, ss, s15 = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    with np.errstate(invalid='ignore', divide='ignore'):
        mean = s / n
        var = (ss - n * mean ** 2) / (n - 1)
        t = mean / np.sqrt(var / n)
    return pd.DataFrame({'n': n, 'mean': mean, 't': t, 'mean15': s15 / n, 'win': a[:, 4] / n},
                        index=[vname(v) for v in VARIANTS])


def dev():
    acc, _ = load_acc()
    t = summary(acc, 'DEV')
    pd.set_option('display.width', 220)
    print(f'DEV 2021–2023: {len(t)} вариантов, со сделками {int((t.n > 0).sum())}')
    print(f'в плюсе {int((t["mean"] > 0).sum())}, с t ≥ 2 {int((t.t >= 2).sum())}, '
          f'с ≥ 300 сделок {int((t.n >= 300).sum())}')
    print('\nлучшие 25 по среднему R (≥ 300 сделок):')
    print(t[t.n >= 300].sort_values('mean', ascending=False).head(25).round(3).to_string())
    # соседи: варианты, отличающиеся ровно одним измерением
    arr = np.array(VARIANTS, dtype=object)
    means = t['mean'].to_numpy()
    cand = []
    for k in np.flatnonzero((t.n >= 300) & (t['mean'] > 0) & (t.t >= 2)):
        diff = (arr != arr[k]).sum(1)
        nb = np.flatnonzero(diff == 1)
        nb = nb[~np.isnan(means[nb])]
        share = (means[nb] >= 0).mean() if len(nb) else 0.0
        cand.append((t.index[k], t.iloc[k]['mean'], t.iloc[k].t, int(t.iloc[k].n), share))
    cand.sort(key=lambda c: -c[2])
    print(f'\nкандидаты (≥ 300 сделок, R > 0, t ≥ 2): {len(cand)}')
    for c in cand[:15]:
        print(f'  {c[0]:40s} {c[1]:+.3f}R t={c[2]:.2f} n={c[3]} соседей не в минусе {c[4]:.0%}')
    chosen = [c[0] for c in cand if c[4] >= 0.6][:3]
    print('\nна VAL идут:', chosen or 'никто')
    # фон: сколько «прибыльных» дал бы чистый случай при таком числе вариантов
    m = t[t.n >= 300]
    print(f'\nсреди {len(m)} вариантов с ≥ 300 сделками: средний R по всем {m["mean"].mean():+.3f}, '
          f'медиана {m["mean"].median():+.3f}, доля в плюсе {(m["mean"] > 0).mean():.1%}')
    for dim in NAMES:
        k = NAMES.index(dim)
        g = pd.Series(m['mean'].to_numpy(), index=[v.split('/')[k] for v in m.index]).groupby(level=0).mean()
        print(f'  {dim:7s}', ' '.join(f'{i}={x:+.3f}' for i, x in g.items()))


def accept(periods, variants):
    acc, per_pair = load_acc()
    target = periods[-1]
    for name in variants:
        v = VARIANTS[[vname(u) for u in VARIANTS].index(name)]
        tr = pd.concat([run_pair(p, only=v) for p in per_pair], ignore_index=True)
        tr['period'] = D.period_of(tr.t.to_numpy())
        tr = tr[tr.period.isin(periods)]
        tr['year'] = pd.to_datetime(tr.t * 60, unit='s', utc=True).dt.year
        g = tr[tr.period == target]
        r = g.r_net.to_numpy()
        n = len(r)
        mean = r.mean() if n else np.nan
        tt = mean / (r.std(ddof=1) / np.sqrt(n)) if n > 2 else np.nan
        lo, hi = boot_ci(g.t.to_numpy(), r)
        pairs = g.groupby('pair').r_net.agg(['size', 'mean'])
        pairs = pairs[pairs['size'] >= 10]
        share = (pairs['mean'] > 0).mean() if len(pairs) else np.nan
        years = tr.groupby('year').r_net.mean()
        c1 = n > 0 and mean > 0 and (lo > 0 or (n >= 300 and tt > 2 and lo >= 0))
        c2 = n > 0 and g.r_net15.mean() > 0
        c3 = share >= 0.55
        c4 = bool((years >= -0.05).all())
        print(f'{name} на {target}: n={n} {mean:+.3f}R [{lo:+.3f}; {hi:+.3f}] t={tt:.2f}, '
              f'×1.5 {g.r_net15.mean():+.3f}, пар в плюсе {share:.0%}, годы {years.round(3).to_dict()}')
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
        pairs = a.pairs or D.bybit_pairs()
        with Pool(a.workers) as pool:
            for pair, msg in pool.imap_unordered(_run, pairs):
                print(pair, msg, flush=True)
    elif a.cmd == 'dev':
        dev()
    elif a.cmd == 'val':
        accept(('DEV', 'VAL'), a.variants)
    else:
        if os.getenv('LUX_OPEN_HOLD') != '1':
            raise SystemExit('HOLD закрыт до согласия владельца (LUX_OPEN_HOLD=1)')
        accept(('DEV', 'VAL', 'HOLD'), a.variants)


if __name__ == '__main__':
    main()
