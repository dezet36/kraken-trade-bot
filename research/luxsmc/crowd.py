"""
Входы LuxAlgo + фильтр толпы по фандингу (PROTOCOL_CROWD.md, записан до
расчёта): 12 входов × {без фильтра, порог 0, порог −1 б.п.}.

    python research/luxsmc/crowd.py run [--workers 2]   # → results/luxsmc_crowd/<пара>.pkl
    python research/luxsmc/crowd.py report               # DEV — все, VAL — все (для понимания) + приёмка
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
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from smcz import data as D                  # noqa: E402
from smcz import sim                        # noqa: E402
from smcz.analyze import boot_ci            # noqa: E402
import lux                                  # noqa: E402
from grid import HIGHER, HOLD_MIN, MIN_R, MIN_STOP, STOP_BUF, nearest_batch   # noqa: E402
from mtf import levels                      # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), 'results', 'luxsmc_crowd')
ENTRY_TF = ('1h', '4h')
DIRS = ('none', 'Es')
TARGETS = ('2R', '3R', 'F')
CROWD = {'C0': None, 'C1': 0.0, 'C2': -1.0}
EXPIRY_BARS = 24


def funding_bp(pair, minutes):
    """Последняя выплаченная ставка к минуте, б.п. за 8 ч; nan — истории нет."""
    path = os.path.join(D.CACHE, 'funding_bybit', f'{pair}.npz')
    if not os.path.exists(path):
        return np.full(len(minutes), np.nan)
    with np.load(path) as z:
        ft, rate = z['t'], z['rate']
    iv = np.r_[480, np.diff(ft)].astype(np.float64)
    k = np.searchsorted(ft, minutes, side='right') - 1
    bp = rate * 1e4 * 480.0 / iv
    return np.where(k >= 0, bp[np.clip(k, 0, None)], np.nan)


def run_pair(pair):
    path = os.path.join(OUT, f'{pair}.pkl')
    if os.path.exists(path):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, tfs=('15m', '1h', '4h', '1d'), src='bybit')
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    rows = []
    for E in ENTRY_TF:
        BE, H = bars[E], HIGHER[E]
        x = pd.DataFrame(lux.build(BE.h, BE.l, BE.c))
        piv = []
        lux.build(bars[H].h, bars[H].l, bars[H].c, pivots=piv)
        lv = levels(bars[H], bars['15m'], piv)
        x = x[x.scope == 'internal']
        side = x.side.to_numpy(np.int64)
        long = side == 1
        bi = x.i.to_numpy(np.int64)
        t_place = BE.t[bi] + BE.tf
        i_place = BE.i1[bi].astype(np.int64)
        hi, lo = x.ob_hi.to_numpy(np.float64), x.ob_lo.to_numpy(np.float64)
        entry = np.where(long, hi, lo)
        stop = np.where(long, lo * (1 - STOP_BUF), hi * (1 + STOP_BUF))
        risk = np.abs(entry - stop)
        ok = risk / entry >= MIN_STOP
        fbp = funding_bp(pair, t_place)
        dirs = {'none': np.ones(len(x), bool), 'Es': x.swing_bias.to_numpy() == side}
        crowd = {}
        for ck, thr in CROWD.items():
            if thr is None:
                crowd[ck] = np.ones(len(x), bool)
            else:
                with np.errstate(invalid='ignore'):
                    passed = np.where(long, fbp <= thr, fbp >= -thr)
                crowd[ck] = np.isnan(fbp) | passed          # нет ставки — фильтр пропускается, как в боте
        for tg in TARGETS:
            if tg == 'F':
                arr = [(s, lv[s]['F']) for s in (1, -1)]
                target = nearest_batch(t_place.astype(np.float64), entry, MIN_R * risk, side,
                                       np.concatenate([a[:, 0] for _, a in arr]),
                                       np.concatenate([a[:, 1] for _, a in arr]),
                                       np.concatenate([a[:, 2] for _, a in arr]),
                                       np.concatenate([np.full(len(a), s, np.int64) for s, a in arr]))
            else:
                target = entry + side * (2.0 if tg == '2R' else 3.0) * risk
            valid = np.flatnonzero(ok & ~np.isnan(target))
            if not len(valid):
                continue
            od = {'side': side[valid], 'i_place': i_place[valid],
                  'i_expire': i_place[valid] + EXPIRY_BARS * BE.tf, 'entry': entry[valid],
                  'stop': stop[valid], 'target': target[valid],
                  'hold': np.full(len(valid), HOLD_MIN[E], np.int64)}
            sub = valid[np.argsort(od['i_place'], kind='stable')]
            r = sim.run(m1, od, exclusive=False, cumf=cumf)
            st, end, ip = (r[k].astype(np.int64) for k in ('status', 'end_i', 'i_place'))
            r15 = r['r_gross'] - 1.5 * r['fee_r'] - r['fund_r']
            for dk, ck in itertools.product(DIRS, CROWD):
                idx = np.flatnonzero((dirs[dk] & crowd[ck])[sub])
                if not len(idx):
                    continue
                keep = sim.one_at_a_time(ip[idx], end[idx], st[idx])
                take = idx[keep & (st[idx] == sim.ST_FILLED)]
                rows.append(pd.DataFrame({
                    'E': E, 'dir': dk, 'target': tg, 'crowd': ck,
                    't': m1.t[ip[take]], 'side': r['side'][take].astype(np.int8),
                    'fund_bp': fbp[sub][take].astype(np.float32),
                    'r_net': r['r_net'][take].astype(np.float32), 'r_net15': r15[take].astype(np.float32)}))
    res = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    res['pair'] = pair
    os.makedirs(OUT, exist_ok=True)
    pd.to_pickle(res, path + '.tmp')
    os.replace(path + '.tmp', path)
    return pair, f'{time.time() - t0:.0f} с, сделок {len(res)}'


def load(reveal):
    if 'HOLD' in reveal and os.getenv('LUX_OPEN_HOLD') != '1':
        raise SystemExit('HOLD закрыт до согласия владельца (LUX_OPEN_HOLD=1)')
    x = pd.concat([pd.read_pickle(os.path.join(OUT, f)) for f in sorted(os.listdir(OUT)) if f.endswith('.pkl')],
                  ignore_index=True)
    x['period'] = D.period_of(x.t.to_numpy())
    x = x[x.period.isin(reveal)].copy()
    x['variant'] = x.E + '/' + x.dir + '/' + x.target + '/' + x.crowd
    x['base'] = x.E + '/' + x.dir + '/' + x.target
    x['year'] = pd.to_datetime(x.t * 60, unit='s', utc=True).dt.year
    return x


def table(x):
    g = x.groupby('variant').r_net
    t = pd.DataFrame({'n': g.size(), 'mean': g.mean(), 'win': g.apply(lambda r: (r > 0).mean())})
    t['t'] = g.mean() / (g.std(ddof=1) / np.sqrt(g.size()))
    return t


def report():
    pd.set_option('display.width', 200)
    x = load(('DEV', 'VAL'))
    dev, val = x[x.period == 'DEV'], x[x.period == 'VAL']
    td, tv = table(dev), table(val)
    for name, t in (('DEV 2021–2023', td), ('VAL 2024-01…2025-03 (для понимания; приёмка — только отобранным)', tv)):
        wide = t['mean'].rename(lambda v: v).to_frame()
        wide['base'] = [v.rsplit('/', 1)[0] for v in wide.index]
        wide['crowd'] = [v.rsplit('/', 1)[1] for v in wide.index]
        n = t['n'].to_frame().assign(base=wide.base, crowd=wide.crowd)
        print(f'\n{name}: средний R (сделок)')
        m = wide.pivot(index='base', columns='crowd', values='mean').round(3)
        k = n.pivot(index='base', columns='crowd', values='n').astype('Int64')
        print(m.astype(str).add(' (').add(k.astype(str)).add(')').to_string())
    cand = td[(td.index.str.endswith(('C1', 'C2'))) & (td.n >= 200) & (td['mean'] > 0) & (td.t >= 2)]
    chosen = cand.sort_values('t', ascending=False).index[:3].tolist()
    print('\nкандидаты на DEV (C1/C2, ≥ 200 сделок, R > 0, t ≥ 2):', chosen or 'нет')
    for v in chosen:
        g = val[val.variant == v]
        r = g.r_net.to_numpy()
        lo, hi = boot_ci(g.t.to_numpy(), r)
        pairs = g.groupby('pair').r_net.agg(['size', 'mean'])
        pairs = pairs[pairs['size'] >= 10]
        share = (pairs['mean'] > 0).mean() if len(pairs) else np.nan
        years = x[x.variant == v].groupby('year').r_net.mean()
        tt = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 else np.nan
        c1 = len(r) > 0 and r.mean() > 0 and (lo > 0 or (len(r) >= 300 and tt > 2 and lo >= 0))
        c2, c3, c4 = g.r_net15.mean() > 0, share >= 0.55, bool((years >= -0.05).all())
        print(f'  {v} на VAL: n={len(r)} {r.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}] ×1.5 {g.r_net15.mean():+.3f}, '
              f'пар в плюсе {share:.0%}, годы {years.round(3).to_dict()} → '
              f'{"ПРИНЯТ" if c1 and c2 and c3 and c4 else "не принят"}')
    # где фильтр помогает: по годам, все 12 входов вместе
    print('\nвсе 12 входов вместе, средний R по годам:')
    y = x.groupby(['year', 'crowd']).r_net.agg(['size', 'mean']).round(3).unstack('crowd')
    print(y.to_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('run', 'report'))
    ap.add_argument('--workers', type=int, default=2)
    ap.add_argument('--pairs', nargs='*')
    a = ap.parse_args()
    if a.cmd == 'run':
        pairs = a.pairs or D.bybit_pairs()
        with Pool(a.workers) as pool:
            for pair, msg in pool.imap_unordered(run_pair, pairs):
                print(pair, msg, flush=True)
    else:
        report()


if __name__ == '__main__':
    main()
