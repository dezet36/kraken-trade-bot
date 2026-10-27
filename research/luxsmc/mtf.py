"""
Вход от внутреннего блока 15м по тренду 4ч, первая цель — ликвидность или
имбаланс 4ч, остаток — по структуре (PROTOCOL_MTF.md, записан до расчёта).

    python research/luxsmc/mtf.py run [--workers 2] [--pairs ...]   # → results/luxsmc_mtf/<пара>.pkl
    python research/luxsmc/mtf.py dev                                 # 18 вариантов на DEV + отбор
    python research/luxsmc/mtf.py val S/LF/X15 ...                    # приёмка на VAL
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

OUT = os.path.join(os.path.dirname(HERE), 'results', 'luxsmc_mtf')
BIAS = ('S', 'I')
TARGETS = ('L', 'F', 'LF')
RUNNERS = ('X0', 'X15', 'X4')
STOP_BUF, MIN_STOP = 0.001, 0.0025
EXPIRY_MIN = 24 * 15
HOLD_MIN = 7 * 1440
TP1_MIN_R = 1.0
FAR = 1000.0


# ── Уровни 4ч: ликвидность и имбалансы ─────────────────────────────────────

def first_cross(x, start, value, above, strict):
    """Первый индекс j ≥ start, где x[j] за value (above — сверху). len(x) — нет."""
    n, j, step = len(x), start, 512
    while j < n:
        seg = x[j:j + step]
        if above:
            hit = seg > value if strict else seg >= value
        else:
            hit = seg < value if strict else seg <= value
        k = np.flatnonzero(hit)
        if len(k):
            return j + int(k[0])
        j += step
        step = min(step * 4, 1 << 20)
    return n


def fvg_4h(B4):
    """Имбалансы 4ч по LuxAlgo: (kind, edge, известен с минуты). Для целей:
    медвежий гэп — цель лонга, ближний край — high бара гэпа; бычий — цель
    шорта, ближний край — low бара гэпа."""
    o, h, l, c = B4.o, B4.h, B4.l, B4.c
    n = len(c)
    out = []
    delta = np.full(n, np.nan)
    delta[1:] = (c[:-1] - o[:-1]) / (o[:-1] * 100)
    cum = np.nancumsum(np.abs(np.nan_to_num(delta)))
    for i in range(2, n):
        thr = cum[i] / i * 2
        if l[i] > h[i - 2] and c[i - 1] > h[i - 2] and delta[i] > thr:
            out.append((-1, l[i], B4.t[i] + B4.tf))        # бычий: цель шорта
        if h[i] < l[i - 2] and c[i - 1] < l[i - 2] and -delta[i] > thr:
            out.append((1, h[i], B4.t[i] + B4.tf))         # медвежий: цель лонга
    return out


def levels(B4, B15, pivots4):
    """Цели по сторонам сделки: {side: {'L': (level, known, gone), 'F': ...}}.
    known — минута, с которой уровень известен; gone — минута закрытия
    15-минутной свечи, которая его вынесла (ликвидность) или коснулась ближнего
    края (имбаланс)."""
    t15c = B15.t + B15.tf
    out = {1: {}, -1: {}}

    def track(items, strict):
        by = {1: [], -1: []}
        for side, level, known in items:
            j0 = int(np.searchsorted(B15.t, known))
            if side == 1:
                j = first_cross(B15.h, j0, level, True, strict)
            else:
                j = first_cross(B15.l, j0, level, False, strict)
            gone = t15c[j] if j < len(t15c) else np.iinfo(np.int64).max
            by[side].append((level, known, gone))
        return {s: np.array(v, dtype=np.float64).reshape(-1, 3) for s, v in by.items()}

    piv = [(p['kind'], p['level'], B4.t[p['i']] + B4.tf) for p in pivots4 if p['scope'] == 'internal']
    liq = track(piv, strict=True)          # вершина над лонгом: вынос — свеча выше уровня
    gaps = track(fvg_4h(B4), strict=False)  # гэп: касание ближнего края
    for s in (1, -1):
        out[s]['L'] = liq[s]
        out[s]['F'] = gaps[s]
        out[s]['LF'] = np.vstack([liq[s], gaps[s]])
    return out


def nearest(cands, t, entry, risk, side):
    """Ближайший живой уровень в сторону сделки не ближе TP1_MIN_R."""
    if not len(cands):
        return np.nan
    lv, kn, gn = cands[:, 0], cands[:, 1], cands[:, 2]
    ok = (kn <= t) & (gn > t) & ((lv - entry) * side >= TP1_MIN_R * risk)
    if not ok.any():
        return np.nan
    d = np.abs(lv[ok] - entry)
    return lv[ok][int(np.argmin(d))]


# ── Прогон пары ─────────────────────────────────────────────────────────────

def bias_at(ev4, scope, minutes, B4):
    """Тренд 4ч (последний слом нужного вида) на минуты minutes."""
    x = ev4[ev4.scope == scope]
    if not len(x):
        return np.zeros(len(minutes), np.int64)
    eff = B4.t[x.i.to_numpy()] + B4.tf
    k = np.searchsorted(eff, minutes, side='right') - 1
    side = x.side.to_numpy()
    return np.where(k >= 0, side[np.clip(k, 0, None)], 0)


def deadlines(break_idx, break_side, after_idx, pos_side, n):
    """Минутка закрытия первого слома против позиции после after_idx."""
    out = np.full(len(after_idx), n - 1, np.int64)
    for s in (1, -1):
        bi = break_idx[break_side == -s]
        m = pos_side == s
        k = np.searchsorted(bi, after_idx[m], side='right')
        out[m] = np.where(k < len(bi), bi[np.clip(k, 0, len(bi) - 1)], n - 1)
    return out


def run_pair(pair):
    out_path = os.path.join(OUT, f'{pair}.pkl')
    if os.path.exists(out_path):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, tfs=('15m', '4h'), src='bybit')
    m1, B15, B4 = bars['base'], bars['15m'], bars['4h']
    n = len(m1.o)
    cumf = D.funding_cum(pair, 'bybit', m1.t)
    ev15 = pd.DataFrame(lux.build(B15.h, B15.l, B15.c))
    piv4 = []
    ev4 = pd.DataFrame(lux.build(B4.h, B4.l, B4.c, pivots=piv4))
    tgt_levels = levels(B4, B15, piv4)

    # сетапы входа: внутренние сломы 15м с новым блоком
    x = ev15[ev15.scope == 'internal']
    side = x.side.to_numpy(np.int64)
    long = side == 1
    entry = np.where(long, x.ob_hi, x.ob_lo).astype(np.float64)
    stop = np.where(long, x.ob_lo * (1 - STOP_BUF), x.ob_hi * (1 + STOP_BUF)).astype(np.float64)
    risk = np.abs(entry - stop)
    t_place = B15.t[x.i.to_numpy()] + B15.tf
    i_place = B15.i1[x.i.to_numpy()].astype(np.int64)
    base_ok = risk / entry >= MIN_STOP
    bias = {'S': bias_at(ev4, 'swing', t_place, B4), 'I': bias_at(ev4, 'internal', t_place, B4)}

    # сломы для выхода остатка: минутка закрытия бара слома
    sw15 = ev15[ev15.scope == 'swing']
    br15 = (B15.i1[sw15.i.to_numpy()] - 1, sw15.side.to_numpy())
    in4 = ev4[ev4.scope == 'internal']
    br4 = (B4.i1[in4.i.to_numpy()] - 1, in4.side.to_numpy())

    rows = []
    for bk, tk in itertools.product(BIAS, TARGETS):
        sel_mask = base_ok & (bias[bk] == side)
        sel = np.flatnonzero(sel_mask)
        tp1 = np.array([nearest(tgt_levels[side[q]][tk], t_place[q], entry[q], risk[q], side[q]) for q in sel])
        good = ~np.isnan(tp1)
        sel, tp1 = sel[good], tp1[good]
        if not len(sel):
            continue
        od = {'side': side[sel], 'i_place': i_place[sel], 'i_expire': i_place[sel] + EXPIRY_MIN,
              'entry': entry[sel], 'stop': stop[sel], 'target': tp1,
              'hold': np.full(len(sel), HOLD_MIN, np.int64)}
        a = sim.run(m1, od, exclusive=False, cumf=cumf)
        filled = a['status'] == sim.ST_FILLED
        for rk in RUNNERS:
            r_net = a['r_net'].copy()
            r15 = (a['r_gross'] - 1.5 * a['fee_r'] - a['fund_r'])
            end = a['end_i'].copy()
            if rk != 'X0':
                took = filled & (a['ex_type'] == sim.EX_TARGET)
                bi, bs = br15 if rk == 'X15' else br4
                dl = deadlines(bi, bs, a['end_i'], a['side'], n)
                odb = {k: a[k] for k in ('side', 'i_place', 'i_expire', 'entry', 'stop', 'hold')}
                odb['target'] = a['entry'] + a['side'] * FAR * np.abs(a['entry'] - a['stop'])
                odb['i_deadline'] = np.where(took, dl, n - 1)
                b = sim.run(m1, odb, exclusive=False, cumf=cumf)
                rb = np.where(took, b['r_net'], a['r_net'])
                rb15 = np.where(took, b['r_gross'] - 1.5 * b['fee_r'] - b['fund_r'], r15)
                r_net = 0.5 * a['r_net'] + 0.5 * rb
                r15 = 0.5 * r15 + 0.5 * rb15
                end = np.where(took, np.maximum(a['end_i'], b['end_i']), a['end_i'])
            keep = sim.one_at_a_time(a['i_place'].astype(np.int64), end.astype(np.int64),
                                     a['status'].astype(np.int64))
            take = keep & filled
            rows.append(pd.DataFrame({
                'bias': bk, 'target': tk, 'runner': rk,
                't': m1.t[a['i_place'][take]], 'side': a['side'][take].astype(np.int8),
                'ex_a': a['ex_type'][take].astype(np.int8),
                'tp1_r': (np.abs(a['target'] - a['entry']) / np.abs(a['entry'] - a['stop']))[take].astype(np.float32),
                'r_net': r_net[take].astype(np.float32), 'r_net15': r15[take].astype(np.float32),
                'stop_pct': (np.abs(a['entry'] - a['stop']) / a['entry'])[take].astype(np.float32),
            }))
    res = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    res['pair'] = pair
    os.makedirs(OUT, exist_ok=True)
    tmp = out_path + '.tmp'
    pd.to_pickle({'trades': res, 'funding': cumf is not None}, tmp)
    os.replace(tmp, out_path)
    return pair, f'{time.time() - t0:.0f} с, сделок {len(res)}'


# ── Разбор ──────────────────────────────────────────────────────────────────

def load(reveal):
    if 'HOLD' in reveal and os.getenv('LUX_OPEN_HOLD') != '1':
        raise SystemExit('HOLD закрыт до согласия владельца (LUX_OPEN_HOLD=1)')
    parts = []
    for f in sorted(os.listdir(OUT)):
        if f.endswith('.pkl'):
            x = pd.read_pickle(os.path.join(OUT, f))['trades']
            if len(x):
                x = x.assign(period=D.period_of(x.t.to_numpy()))
                parts.append(x[x.period.isin(reveal)])
    x = pd.concat(parts, ignore_index=True)
    x['variant'] = x.bias + '/' + x.target + '/' + x.runner
    x['year'] = pd.to_datetime(x.t * 60, unit='s', utc=True).dt.year
    return x


def stats(g):
    r = g.r_net.to_numpy(np.float64)
    n = len(r)
    return {'n': n, 'mean': r.mean() if n else np.nan, 'sum': r.sum(),
            'win': (r > 0).mean() if n else np.nan,
            't': r.mean() / (r.std(ddof=1) / np.sqrt(n)) if n > 2 else np.nan,
            'mean15': g.r_net15.mean(), 'tp1_r': g.tp1_r.median(),
            'stop_med': g.stop_pct.median() * 100}


def dev():
    x = load(('DEV',))
    t = pd.DataFrame({v: stats(g) for v, g in x.groupby('variant')}).T
    pd.set_option('display.width', 200)
    print('DEV 2021–2023 (R на сделку после издержек; tp1_r — медиана первой цели в R):')
    print(t.sort_values('mean', ascending=False).astype(float).round(3).to_string())
    t['grp'] = [v.split('/')[0] for v in t.index]
    chosen = []
    for v, row in t[t.n >= 200].sort_values('mean', ascending=False).iterrows():
        nb = t[(t.grp == row.grp) & (t.index != v)]
        share = (nb['mean'] >= 0).mean()
        if row['mean'] > 0 and share >= 0.5 and len(chosen) < 3:
            chosen.append(v)
    print('\nна VAL идут:', chosen or 'никто (нет варианта с плюсом и соседями)')
    for v in chosen:
        print(f'  {v} по годам:', x[x.variant == v].groupby('year').r_net.agg(['size', 'mean']).round(3).to_dict('index'))
    print('\nвыход первой половины (DEV, все варианты): стоп {:.0%}, цель {:.0%}, время {:.0%}'.format(
        (x.ex_a == sim.EX_STOP).mean(), (x.ex_a == sim.EX_TARGET).mean(), (x.ex_a == sim.EX_TIME).mean()))
    print('лонги {:+.3f}R, шорты {:+.3f}R'.format(x[x.side == 1].r_net.mean(), x[x.side == -1].r_net.mean()))


def accept(periods, variants):
    x = load(periods)
    target = periods[-1]
    for v in variants:
        g = x[(x.variant == v) & (x.period == target)]
        s = stats(g)
        lo, hi = boot_ci(g.t.to_numpy(), g.r_net.to_numpy())
        pairs = g.groupby('pair').r_net.agg(['size', 'mean'])
        pairs = pairs[pairs['size'] >= 10]
        share = (pairs['mean'] > 0).mean() if len(pairs) else np.nan
        years = x[x.variant == v].groupby('year').r_net.mean()
        c1 = s['n'] > 0 and s['mean'] > 0 and (lo > 0 or (s['n'] >= 300 and s['t'] > 2 and lo >= 0))
        c2 = s['n'] > 0 and s['mean15'] > 0
        c3 = share >= 0.55
        c4 = bool((years >= -0.05).all())
        print(f'{v} на {target}: n={s["n"]} {s["mean"]:+.3f}R [{lo:+.3f}; {hi:+.3f}] t={s["t"]:.2f}, '
              f'×1.5 {s["mean15"]:+.3f}, пар в плюсе {share:.0%}, годы {years.round(3).to_dict()}')
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
            for pair, msg in pool.imap_unordered(run_pair, pairs):
                print(pair, msg, flush=True)
    elif a.cmd == 'dev':
        dev()
    elif a.cmd == 'val':
        accept(('DEV', 'VAL'), a.variants)
    else:
        accept(('DEV', 'VAL', 'HOLD'), a.variants)


if __name__ == '__main__':
    main()
