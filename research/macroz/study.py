"""
Импульс после макро-новостей США (PROTOCOL.md, записан до расчёта).

    python research/macroz/study.py check      # всплеск в минуту выхода — проверка времени
    python research/macroz/study.py dev        # 144 варианта × BTC/ETH на DEV + отбор
    python research/macroz/study.py val V ...  # приёмка на VAL (V = kind/k/h/dir/thr)
    MACRO_OPEN_HOLD=1 python research/macroz/study.py hold V ...

HOLD без MACRO_OPEN_HOLD=1 не считается вовсе.
"""
from __future__ import annotations

import itertools
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from numba import njit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from smcz import data as D                   # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), 'results', 'macroz')
INSTR = ('BTCUSDT', 'ETHUSDT')
KINDS = ('CPI', 'NFP', 'FOMC', 'ALL')
KS, HOLDS, DIRS, THRS = (1, 3, 5), (15, 60, 240), ('cont', 'fade'), (0.0, 0.002)
COST = 0.00055 + 0.0003                       # тейкер + проскальзывание на сторону
PERIODS = {'DEV': ('2021-01-01', '2024-01-01'), 'VAL': ('2024-01-01', '2025-04-01'),
           'HOLD': ('2025-04-01', '2026-10-01')}
NY = ZoneInfo('America/New_York')
LOCAL = {'CPI': (8, 30), 'NFP': (8, 30), 'FOMC': (14, 0)}


def period_of(minute):
    for name, (a, b) in PERIODS.items():
        if D.minute(a) <= minute < D.minute(b):
            return name
    return ''


def events():
    ev = pd.read_csv(os.path.join(HERE, 'events.csv'))
    ev['period'] = [period_of(m) for m in ev.minute]
    return ev


def placebo(kind, ev):
    """Те же 08:30 / 14:00 по Нью-Йорку в будни без событий (любых)."""
    busy = set(ev.date_ny)
    hm = LOCAL[kind]
    out = []
    d = date(2021, 1, 4)
    while d < date(2026, 10, 1):
        if d.weekday() < 5 and d.isoformat() not in busy:
            local = datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=NY)
            m = int(local.astimezone(ZoneInfo('UTC')).timestamp() // 60)
            out.append((d.isoformat(), m))
        d += timedelta(days=1)
    pl = pd.DataFrame(out, columns=['date_ny', 'minute'])
    pl['period'] = [period_of(m) for m in pl.minute]
    return pl


@njit(cache=True)
def trades(o, h, l, c, idx, k, hold, fade, thr, cost):
    """Чистая доходность (доля цены) и R по каждой точке idx (минута выхода новости)."""
    n = len(idx)
    net = np.full(n, np.nan)
    rr = np.full(n, np.nan)
    for q in range(n):
        i = idx[q]
        if i < 1 or i + k + hold >= len(c):
            continue
        p0 = c[i - 1]
        rk = np.log(c[i + k - 1] / p0)
        if rk == 0.0 or abs(rk) < thr:
            continue
        up = rk > 0
        s = 1.0 if up else -1.0
        if fade:
            s = -s
        e = o[i + k]
        if fade:
            if up:
                stop = h[i:i + k].max() * 1.0005
            else:
                stop = l[i:i + k].min() * 0.9995
        else:
            stop = p0
        risk = (e - stop) * s
        if risk <= 0:
            continue                                  # к входу импульс уже откатился за стоп
        x = c[i + k + hold - 1]
        for j in range(i + k, i + k + hold):
            if s > 0 and l[j] <= stop:
                x = min(stop, o[j])
                break
            if s < 0 and h[j] >= stop:
                x = max(stop, o[j])
                break
        g = s * (x / e - 1.0) - 2.0 * cost
        net[q] = g
        rr[q] = g / (risk / e)
    return net, rr


_bars = {}


def bars(pair):
    if pair not in _bars:
        m = D.load_1m(pair, 'bybit')
        _bars[pair] = (m.t, m.o, m.h, m.l, m.c)
    return _bars[pair]


def index_of(pair, minutes):
    t = bars(pair)[0]
    i = np.searchsorted(t, minutes)
    ok = (i < len(t)) & (t[np.minimum(i, len(t) - 1)] == minutes)
    return np.where(ok, i, -1).astype(np.int64)


def run_variant(pair, idx, k, hold, direction, thr, cost=COST):
    _, o, h, l, c = bars(pair)
    return trades(o, h, l, c, idx, k, hold, direction == 'fade', thr, cost)


def variants():
    return list(itertools.product(KINDS, KS, HOLDS, DIRS, THRS))


def vname(v):
    kind, k, h, d, thr = v
    return f'{kind}/k{k}/h{h}/{d}/{"thr" if thr else "all"}'


def table(ev, periods):
    """Сделки всех вариантов по событиям выбранных периодов."""
    ev = ev[ev.period.isin(periods)]
    rows = []
    for pair in INSTR:
        idx_all = index_of(pair, ev.minute.to_numpy())
        for v in variants():
            kind, k, h, d, thr = v
            sel = np.ones(len(ev), bool) if kind == 'ALL' else (ev.kind == kind).to_numpy()
            net, rr = run_variant(pair, idx_all[sel], k, h, d, thr)
            net15, _ = run_variant(pair, idx_all[sel], k, h, d, thr, COST * 1.5)
            for p, a, b, c_ in zip(ev.period.to_numpy()[sel], net, rr, net15):
                if a == a:
                    rows.append((vname(v), pair, p, a, b, c_))
    return pd.DataFrame(rows, columns=['variant', 'pair', 'period', 'net', 'r', 'net15'])


def stats(x):
    n = len(x)
    if n < 2:
        return n, np.nan, np.nan
    return n, x.mean(), x.mean() / (x.std(ddof=1) / np.sqrt(n))


def check():
    """Всплеск размаха BTC в минуту выхода против медианы часа до неё."""
    ev = events()
    t, o, h, l, c = bars('BTCUSDT')
    i = index_of('BTCUSDT', ev.minute.to_numpy())
    rng = (h - l) / c
    ratio = []
    for q in i:
        if q < 61:
            ratio.append(np.nan)
            continue
        ratio.append(max(rng[q], rng[q + 1]) / np.median(rng[q - 60:q]))
    ev['spike'] = ratio
    print('всплеск размаха (минута выхода и следующая ÷ медиана часа до):')
    print(ev.groupby('kind').spike.describe(percentiles=[.1, .5]).round(1).to_string())
    weak = ev[ev.spike < 2]
    print(f'\nсобытий без всплеска ×2: {len(weak)}')
    print(weak[['kind', 'utc', 'spike']].to_string(index=False))


def dev():
    ev = events()
    x = table(ev, ('DEV',))
    os.makedirs(OUT, exist_ok=True)
    rows = []
    for v in sorted(x.variant.unique()):
        r = {'variant': v}
        for pair in INSTR:
            n, m, t = stats(x[(x.variant == v) & (x.pair == pair)].net.to_numpy())
            r[f'{pair[:3]}_n'], r[f'{pair[:3]}_mean%'], r[f'{pair[:3]}_t'] = n, m * 100, t
        rows.append(r)
    tb = pd.DataFrame(rows).set_index('variant')
    pd.set_option('display.width', 220)
    tb['t_avg'] = (tb.BTC_t + tb.ETH_t) / 2
    print('DEV 2021–2023, чистая доходность на сделку в % (после издержек 0.17%):')
    print(tb.sort_values('t_avg', ascending=False).head(30).round(3).to_string())
    print(f'\nв плюсе у обоих: {int(((tb["BTC_mean%"] > 0) & (tb["ETH_mean%"] > 0)).sum())} из {len(tb)}; '
          f'средний по всем вариантам BTC {tb["BTC_mean%"].mean():+.3f}%, ETH {tb["ETH_mean%"].mean():+.3f}%')
    # отбор
    def neighbours(v):
        kind, k, h, d, thr = v.split('/')
        k, h = int(k[1:]), int(h[1:])
        out = []
        for kk in KS:
            if abs(KS.index(kk) - KS.index(k)) == 1:
                out.append(f'{kind}/k{kk}/h{h}/{d}/{thr}')
        for hh in HOLDS:
            if abs(HOLDS.index(hh) - HOLDS.index(h)) == 1:
                out.append(f'{kind}/k{k}/h{hh}/{d}/{thr}')
        return out
    cands = []
    for v, r in tb.iterrows():
        if min(r.BTC_n, r.ETH_n) < 20 or not (r['BTC_mean%'] > 0 and r['ETH_mean%'] > 0):
            continue
        if not (r.BTC_t >= 2.5 and r.ETH_t >= 2.5):
            continue
        nb = [u for u in neighbours(v) if u in tb.index]
        ok = np.mean([(tb.loc[u, 'BTC_mean%'] > 0 and tb.loc[u, 'ETH_mean%'] > 0) for u in nb]) if nb else 0
        if ok >= 0.5:
            cands.append((r.t_avg, v))
    chosen = [v for _, v in sorted(cands, reverse=True)[:3]]
    print('\nкандидаты (t ≥ 2.5 у BTC и ETH, соседи в плюсе):', chosen or 'нет')
    tb.to_csv(os.path.join(OUT, 'dev_table.csv'))
    # описание: размер импульса
    print('\nмедиана |импульса| за 5 минут, % (DEV): события', end=' ')
    t, o, h, l, c = bars('BTCUSDT')
    for kind in ('CPI', 'NFP', 'FOMC'):
        e = ev[(ev.kind == kind) & (ev.period == 'DEV')]
        i = index_of('BTCUSDT', e.minute.to_numpy())
        i = i[i > 0]
        r5 = np.abs(np.log(c[i + 4] / c[i - 1])) * 100
        pl = placebo(kind, ev)
        pl = pl[pl.period == 'DEV']
        j = index_of('BTCUSDT', pl.minute.to_numpy())
        j = j[j > 0]
        p5 = np.abs(np.log(c[j + 4] / c[j - 1])) * 100
        print(f'{kind} BTC {np.median(r5):.2f} (плацебо {np.median(p5):.2f})', end='; ')
    print()


def accept(periods, names):
    ev = events()
    x = table(ev, periods)
    target = periods[-1]
    for name in names:
        kind, k, h, d, thr = name.split('/')
        k, h, thr_v = int(k[1:]), int(h[1:]), (0.002 if thr == 'thr' else 0.0)
        ok1 = ok2 = True
        for pair in INSTR:
            g = x[(x.variant == name) & (x.pair == pair) & (x.period == target)]
            n, m, t = stats(g.net.to_numpy())
            m15 = g.net15.mean()
            ok1 &= m > 0
            ok2 &= m15 > 0
            print(f'{name} {pair} {target}: n={n} {m * 100:+.3f}% t={t:.2f}, ×1.5 {m15 * 100:+.3f}%, '
                  f'R {g.r.mean():+.3f}')
        # плацебо: то же правило в ту же минуту по Нью-Йорку в будни без событий
        kinds = ('CPI', 'NFP', 'FOMC') if kind == 'ALL' else (kind,)
        ok3 = True
        for pair in INSTR:
            ev_net = x[(x.variant == name) & (x.pair == pair)].net
            pl_net = []
            for kd in kinds:
                pl = placebo(kd, ev)
                pl = pl[pl.period.isin(periods)]
                net, _ = run_variant(pair, index_of(pair, pl.minute.to_numpy()), k, h, d, thr_v)
                pl_net.append(net[~np.isnan(net)])
            pl_net = np.concatenate(pl_net)
            ok3 &= ev_net.mean() > pl_net.mean()
            print(f'   {pair} все периоды приёмки: события {ev_net.mean() * 100:+.3f}% ({len(ev_net)}) против '
                  f'плацебо {pl_net.mean() * 100:+.3f}% ({len(pl_net)})')
        print(f'   1 {"да" if ok1 else "нет"} · 2 {"да" if ok2 else "нет"} · 3 {"да" if ok3 else "нет"} → '
              f'{"ПРИНЯТ" if ok1 and ok2 and ok3 else "не принят"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'dev'
    if cmd == 'check':
        check()
    elif cmd == 'dev':
        dev()
    elif cmd == 'val':
        accept(('DEV', 'VAL'), sys.argv[2:])
    elif cmd == 'hold':
        if os.getenv('MACRO_OPEN_HOLD') != '1':
            raise SystemExit('HOLD закрыт до согласия владельца (MACRO_OPEN_HOLD=1)')
        accept(('DEV', 'VAL', 'HOLD'), sys.argv[2:])
