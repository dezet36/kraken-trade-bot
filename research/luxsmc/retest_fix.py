"""
«Зона 1ч → слом 15м»: исправление входа и фильтры (PROTOCOL_RETEST_FIX.md,
записан до расчёта). Сетапы — из retest.py; вход MKT / CONF / LIM, фильтры
none / crowd / htf / crowd+htf.

    python research/luxsmc/retest_fix.py run      # → results/luxsmc_retest_fix/<пара>.npz
    python research/luxsmc/retest_fix.py dev
    python research/luxsmc/retest_fix.py val V …
"""
from __future__ import annotations

import argparse
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
import lux                                  # noqa: E402
import retest as RT                         # noqa: E402
from crowd import funding_bp                # noqa: E402

OUT = os.path.join(RESEARCH, 'results', 'luxsmc_retest_fix')
DIMS = {'trend': ('swing', 'internal'), 'poi': ('OB', 'FVG'), 'entry': ('MKT', 'CONF', 'LIM'),
        'stop': ('s15', 's1h'), 'target': ('2R', '3R'), 'filter': ('none', 'crowd', 'htf', 'crowd+htf')}
VARIANTS = list(itertools.product(*DIMS.values()))
INDEX = {v: k for k, v in enumerate(VARIANTS)}
CONF_BARS = 16


def trend_on(ev, B, scope, minutes):
    x = ev[ev.scope == scope]
    if not len(x):
        return np.zeros(len(minutes), np.int64)
    eff = B.t[x.i.to_numpy()] + B.tf
    k = np.searchsorted(eff, minutes, side='right') - 1
    return np.where(k >= 0, x.side.to_numpy()[np.clip(k, 0, None)], 0)


def confirm(B15, j, side, level, stop):
    """Бар подтверждённого ретеста: касание уровня и закрытие обратно за ним
    (не позже CONF_BARS свечей после слома); стоп раньше — None."""
    touched = False
    for k in range(j + 1, min(j + 1 + CONF_BARS, len(B15.c))):
        if (side > 0 and B15.l[k] <= stop) or (side < 0 and B15.h[k] >= stop):
            return None
        if not touched and ((side > 0 and B15.l[k] <= level) or (side < 0 and B15.h[k] >= level)):
            touched = True
        if touched and ((side > 0 and B15.c[k] > level) or (side < 0 and B15.c[k] < level)):
            return k
    return None


def run_pair(pair, only=None):
    t0 = time.time()
    bars = D.load(pair, tfs=('15m', '1h', '4h'), src='bybit')
    m1, B15, B1, B4 = bars['base'], bars['15m'], bars['1h'], bars['4h']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    ev1 = pd.DataFrame(lux.build(B1.h, B1.l, B1.c))
    ev15 = pd.DataFrame(lux.build(B15.h, B15.l, B15.c))
    ev4 = pd.DataFrame(lux.build(B4.h, B4.l, B4.c))
    zs_all = RT.zones(B1, B15, ev1)
    internal15 = ev15[ev15.scope == 'internal']
    t_close_all = B15.t[internal15.i.to_numpy()] + 15
    acc = np.zeros((len(VARIANTS), RT.NDAYS, 3)) if only is None else None
    trades = []
    for tr, pk in itertools.product(DIMS['trend'], DIMS['poi']):
        if only is not None and (tr, pk) != only[:2]:
            continue
        trend = RT.trend_at(ev1, B1, tr, t_close_all)
        ev, zbot, ztop = RT.setups(B15, ev15, zs_all[pk], trend)
        if not len(ev):
            continue
        side = ev.side.to_numpy(np.int64)
        long = side > 0
        j = ev.i.to_numpy()
        level = ev.level.to_numpy(np.float64)
        for en, sp in itertools.product(DIMS['entry'], DIMS['stop']):
            if sp == 's15':
                stop = np.where(long, ev.ob_lo * (1 - RT.BUF), ev.ob_hi * (1 + RT.BUF)).astype(np.float64)
            else:
                stop = np.where(long, zbot * (1 - RT.BUF), ztop * (1 + RT.BUF)).astype(np.float64)
            if en == 'MKT':
                bar = j.copy()
            elif en == 'CONF':
                found = (confirm(B15, jj, s, lv, st) for jj, s, lv, st in zip(j, side, level, stop))
                bar = np.array([-1 if k is None else k for k in found], dtype=np.int64)
            else:
                bar = j.copy()
            have = bar >= 0
            bar_ok = np.where(have, bar, 0)
            ref = np.where(have, B15.c[bar_ok], np.nan)                     # цена решения
            decide = (B15.t[bar_ok] + 15).astype(np.int64)
            i_place = B15.i1[bar_ok].astype(np.int64)
            if en == 'LIM':
                entry, i_exp = level, i_place + RT.ORDER_MIN
                base = (ref - level) * side > 0
            else:
                entry, i_exp = np.full(len(j), np.nan), i_place + 1
                base = np.ones(len(j), bool)
            px = level if en == 'LIM' else ref
            risk = (px - stop) * side
            ok = have & base & (risk > 0) & (risk / px >= RT.MIN_STOP)
            crowd_ok = np.ones(len(j), bool)
            fb = funding_bp(pair, decide)
            with np.errstate(invalid='ignore'):
                crowd_ok = np.isnan(fb) | np.where(long, fb <= 0, fb >= 0)
            htf_ok = trend_on(ev4, B4, 'swing', decide) == side
            for tg in DIMS['target']:
                R = 2.0 if tg == '2R' else 3.0
                target = px + side * R * risk
                sel = np.flatnonzero(ok)
                if not len(sel):
                    continue
                od = {'side': side[sel], 'i_place': i_place[sel], 'i_expire': i_exp[sel],
                      'entry': entry[sel] if en == 'LIM' else np.full(len(sel), np.nan),
                      'stop': stop[sel], 'target': target[sel], 'hold': np.full(len(sel), RT.HOLD_MIN, np.int64)}
                order = np.argsort(i_place[sel], kind='stable')
                r = sim.run(m1, od, exclusive=False, cumf=cumf)
                st, end, ip = (r[k].astype(np.int64) for k in ('status', 'end_i', 'i_place'))
                r15 = r['r_gross'] - 1.5 * r['fee_r'] - r['fund_r']
                masks = {'none': np.ones(len(sel), bool), 'crowd': crowd_ok[sel][order],
                         'htf': htf_ok[sel][order]}
                masks['crowd+htf'] = masks['crowd'] & masks['htf']
                for fl in DIMS['filter']:
                    v = (tr, pk, en, sp, tg, fl)
                    if only is not None and v != only:
                        continue
                    m = np.flatnonzero(masks[fl])
                    if not len(m):
                        continue
                    keep = sim.one_at_a_time(ip[m], end[m], st[m])
                    take = m[keep & (st[m] == sim.ST_FILLED)]
                    day = m1.t[r['fill_i'][take]] // 1440 - RT.DAY0
                    good = (day >= 0) & (day < RT.NDAYS)
                    take, day = take[good], day[good]
                    if only is not None:
                        trades.append(pd.DataFrame({'t': m1.t[r['fill_i'][take]], 'r': r['r_net'][take],
                                                    'r15': r15[take], 'pair': pair, 'ex': r['ex_type'][take],
                                                    'hold_h': (r['end_i'][take] - r['fill_i'][take]) / 60}))
                        continue
                    a = acc[INDEX[v]]
                    a[:, 0] += np.bincount(day, minlength=RT.NDAYS)
                    a[:, 1] += np.bincount(day, r['r_net'][take], minlength=RT.NDAYS)
                    a[:, 2] += np.bincount(day, r15[take], minlength=RT.NDAYS)
    if only is not None:
        return pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f'{pair}.npz')
    np.savez_compressed(path[:-4] + '.tmp.npz', acc=acc.astype(np.float32))
    os.replace(path[:-4] + '.tmp.npz', path)
    return pair, f'{time.time() - t0:.0f} с'


def load(period, pairs=None):
    lo = 0 if period == 'DEV' else RT.EDGES['DEV']
    hi = RT.EDGES['DEV'] if period == 'DEV' else RT.EDGES['VAL'] if period == 'VAL' else RT.NDAYS
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
                        index=['/'.join(v) for v in VARIANTS])


def dev():
    acc, _, ndays = load('DEV')
    tb = clustered(acc)
    acc10, _, _ = load('DEV', RT.POOL10)
    tb['в_мес_10'] = acc10[:, :, 0].sum(1) / (ndays / 30.4)
    tb['R_10'] = acc10[:, :, 1].sum(1) / np.maximum(acc10[:, :, 0].sum(1), 1)
    tb['gross'] = tb['mean'] + 2 * (tb['mean'] - tb['mean15'])
    pd.set_option('display.width', 220)
    print('DEV 2021–2023, 31 пара: R после издержек, t по дням; gross — до комиссий; в_мес_10 — сделок в месяц на 10 парах SMC')
    print(tb.sort_values('t_day', ascending=False).head(30).round(3).to_string())
    m = tb[tb.n >= 300]
    print(f'\nвариантов с ≥ 300 сделками {len(m)}: в плюсе {(m["mean"] > 0).mean():.0%}, средний R {m["mean"].mean():+.3f}')
    for k, name in enumerate(DIMS):
        g = m.groupby([v.split('/')[k] for v in m.index])
        print(f'  {name:7s}', ' '.join(f'{i}: {x["mean"].mean():+.3f} (до изд. {x["gross"].mean():+.3f})'
                                        for i, x in g))
    arr = np.array(VARIANTS)
    cands = []
    for k in np.flatnonzero((tb.n >= 300) & (tb['mean'] > 0) & (tb.t_day >= 2.5)):
        nb = np.flatnonzero((arr != arr[k]).sum(1) == 1)
        share = (tb.iloc[nb]['mean'] >= 0).mean() if len(nb) else 0
        cands.append((tb.iloc[k].t_day, tb.index[k], share))
    cands.sort(reverse=True)
    print('\nкандидаты:', [(v, round(t, 2), f'{sh:.0%}') for t, v, sh in cands] or 'нет')
    print('на VAL идут:', [v for t, v, sh in cands if sh >= 0.5][:3] or 'никто')
    tb.to_csv(os.path.join(OUT, 'dev_table.csv'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('run', 'dev', 'val'))
    ap.add_argument('variants', nargs='*')
    ap.add_argument('--pairs', nargs='*')
    a = ap.parse_args()
    if a.cmd == 'run':
        for pair in a.pairs or D.bybit_pairs():
            path = os.path.join(OUT, f'{pair}.npz')
            print(pair, 'уже есть' if os.path.exists(path) else run_pair(pair)[1], flush=True)
    elif a.cmd == 'dev':
        dev()
    else:
        RT.run_pair = run_pair                     # приёмка — тем же кодом retest.accept
        RT.VARIANTS = VARIANTS
        RT.accept(('DEV', 'VAL'), a.variants)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
