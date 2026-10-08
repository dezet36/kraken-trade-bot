"""
Старая ФИБО: пять идей улучшения — А (вход внутри импульса 12ч), Б (сторона
по BTC), В (поток на откате), Г (выход), Д (сторона по доминации USDT).
Протокол — docs/ФИБО_старая_идеи_2026-10-08.md (закоммичен до расчёта).

Проход 1 — признаки каждой заявки по 9 ячейкам; пороги В — медианы по bear +
mid1 на 10 парах замера. Проход 2 — портфель с правилами брокера по каждой
гипотезе, налив касанием и «насквозь».

    python research/fibo_old_ideas.py   → results/fibo_old_ideas.txt
"""
import copy
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fibo_live_sim as FS                            # noqa: E402
import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
import smc_engine                                     # noqa: E402
from common import ci                                 # noqa: E402
from fibo_crowd import funding_at                     # noqa: E402
from fibo_geometry import CELLS, load_cell            # noqa: E402
from fibo_htf_split import BASE                       # noqa: E402
from fibz.legs import _legs                           # noqa: E402
from smcz import structure as SS                      # noqa: E402
from smcz import data as ZD                           # noqa: E402

H = np.timedelta64(1, 'h')
DOM = pd.read_csv(os.path.join(HERE, 'market_dominance', 'usdt_d_daily.csv'), parse_dates=['day'])
DOM['day'] = DOM.day.dt.tz_convert('UTC').dt.tz_localize(None)
DOM = DOM.set_index('day').usdt_d
BINANCE = set(ZD.PAIRS)


def naive(x):
    t = pd.Timestamp(x)
    return t.tz_convert('UTC').tz_localize(None) if t.tzinfo else t


def legs12(h1):
    """Ноги 12ч (свинги 3+3) из часовых свечей: (dir, conf_close, A, B)."""
    df = h1.set_index(pd.DatetimeIndex(FS.naive(h1['timestamp'])))[['open', 'high', 'low', 'close']]
    d12 = df.resample('12h', origin='epoch').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    cnt = df['close'].resample('12h', origin='epoch').count()
    d12 = d12[cnt == 12]
    hh, ll = d12.high.to_numpy(float), d12.low.to_numpy(float)
    ph, pl = SS.pivots(hh, ll, 3)
    raw = _legs(hh, ll, ph, pl, 3)
    t12 = d12.index.to_numpy()
    return [(int(d), t12[int(c)] + np.timedelta64(12, 'h'), A, B) for d, a, b, c, A, B, _ in raw]


def binance_1m(pair):
    name = '1000SHIBUSDT' if pair == 'SHIB1000USDT' else pair
    if name not in BINANCE:
        return None
    b = ZD.load_1m(name, 'binance')
    return b.t, np.r_[0.0, np.cumsum(b.qv)], np.r_[0.0, np.cumsum(b.tbq)]


def features(cell):
    cache, orders, pairs = load_cell(cell)
    orders = [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT]
    bt.CACHE_DIR = os.path.join(HERE, cache)
    btc = bt.load_pair('BTCUSDT')['1h']
    bt_t = FS.naive(btc['timestamp']) + H                   # момент закрытия часа
    bt_c = btc['close'].to_numpy(float)
    fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in pairs}
    rows = []
    for p in pairs:
        own = [(i, o) for i, o in enumerate(orders) if o.pair == p]
        if not own:
            continue
        data = bt.load_pair(p)
        h1, m5 = data['1h'], data['5m']
        t1 = FS.naive(h1['timestamp'])
        hi1, lo1, v1 = (h1[c].to_numpy(float) for c in ('high', 'low', 'volume'))
        t5 = FS.naive(m5['timestamp'])
        c5 = m5['close'].to_numpy(float)
        lg = legs12(h1)
        lg_close = np.array([x[1] for x in lg])
        bn = binance_1m(p)
        for i, o in own:
            T = np.datetime64(naive(o.created))
            d = 1 if o.direction == 'LONG' else -1
            f = {'i': i, 'dir': d}
            tms = int(pd.Timestamp(T).value // 10 ** 6)
            f['crowd'] = bool(funding_at(fund.get(p), tms) * d <= 0)
            # цена в момент T — закрытие последней 5-минутки до T
            j5 = int(np.searchsorted(t5, T, side='left')) - 1
            price = c5[j5] if j5 >= 0 else np.nan
            # А: последняя подтверждённая нога 12ч
            k = int(np.searchsorted(lg_close, T, side='right')) - 1
            f['a1'] = f['a2'] = False
            if k >= 0:
                ld, lc, A, B = lg[k]
                if ld == d and T - lc <= np.timedelta64(144, 'h'):
                    a0 = int(np.searchsorted(t1, lc, side='left'))
                    a1_ = int(np.searchsorted(t1 + H, T, side='right'))   # закрытые часы до T
                    seg_ok = True
                    if a1_ > a0:
                        seg_ok = (hi1[a0:a1_].max() <= B) if d == 1 else (lo1[a0:a1_].min() >= B)
                    f['a1'] = bool(seg_ok)
                    L = (B - A) * d
                    lo_, hi_ = sorted((B - d * 0.786 * L, B - d * 0.236 * L))
                    f['a2'] = bool(seg_ok and lo_ <= price <= hi_)
            # Б: BTC за 7 и 30 суток
            kb = int(np.searchsorted(bt_t, T, side='right')) - 1
            for name, hrs in (('b7', 168), ('b30', 720)):
                f[name] = np.sign(bt_c[kb] / bt_c[kb - hrs] - 1) if kb - hrs >= 0 else 0.0
            # Д: доминация USDT, значение дня D доступно с D+1
            day = pd.Timestamp(T).normalize() - pd.Timedelta(days=1)
            for name, dd in (('d7', 7), ('d30', 30)):
                try:
                    f[name] = float(np.sign(DOM.loc[day] - DOM.loc[day - pd.Timedelta(days=dd)]))
                except KeyError:
                    f[name] = 0.0
            # В: импульс и откат на часе (по ценам ключа заявки)
            _, _, start_px, end_px = o.key
            kt = int(np.searchsorted(t1 + H, T, side='right'))          # число закрытых часов
            lo_win = max(0, kt - 60)
            ext = hi1 if d == 1 else lo1
            org = lo1 if d == 1 else hi1
            e_idx = next((m for m in range(kt - 1, lo_win - 1, -1) if abs(ext[m] - end_px) <= 1e-12 * end_px), None)
            s_idx = None
            if e_idx is not None:
                s_idx = next((m for m in range(e_idx, lo_win - 1, -1) if abs(org[m] - start_px) <= 1e-12 * start_px), None)
            f['v2'] = np.nan
            f['v1'] = np.nan
            if e_idx is not None and s_idx is not None:
                imp = v1[s_idx:e_idx + 1].mean()
                if kt - 1 > e_idx and imp > 0:
                    f['v2'] = v1[e_idx + 1:kt].mean() / imp
                if bn is not None:
                    bt_m, cq, ct = bn
                    m0 = int(np.searchsorted(bt_m, int((t1[e_idx] + H - np.datetime64('1970-01-01T00:00')) // np.timedelta64(1, 'm'))))
                    m1 = int(np.searchsorted(bt_m, int((T - np.datetime64('1970-01-01T00:00')) // np.timedelta64(1, 'm'))))
                    if m1 > m0 and cq[m1] - cq[m0] > 0:
                        buy = (ct[m1] - ct[m0]) / (cq[m1] - cq[m0])
                        f['v1'] = (1 - buy) if d == 1 else buy           # агрессор против сделки
            rows.append(f)
    return cell, pd.DataFrame(rows).set_index('i').sort_index()


def with_exit(orders, kind):
    out = []
    for o in orders:
        c = copy.copy(o)
        risk = abs(o.entry - o.stop)
        if kind == 'Г1':
            d = 1 if o.direction == 'LONG' else -1
            c.targets = [o.entry + d * risk, o.targets[0]]
            c.fractions = [0.5, 0.5]
        elif kind == 'Г2':
            c.trail_distance = risk
        out.append(c)
    return out


def rules(th):
    side = lambda o, x, key: x[key] == (1 if o.direction == 'LONG' else -1)          # noqa: E731
    return [
        ('база: обе стороны', 'обе', lambda o, x: True, None),
        ('база: F3 (как в боте)', 'F3', lambda o, x: o.direction == 'SHORT' and x['crowd'], None),
        ('А1 живая нога 12ч в ту же сторону', 'обе', lambda o, x: x['a1'], None),
        ('А2 А1 и цена в зоне 0.236–0.786 ноги 12ч', 'обе', lambda o, x: x['a2'], None),
        ('Б1 сторона по BTC за 7 сут', 'обе', lambda o, x: side(o, x, 'b7'), None),
        ('Б2 сторона по BTC за 30 сут', 'обе', lambda o, x: side(o, x, 'b30'), None),
        ('Д1 против доминации USDT за 7 сут', 'обе', lambda o, x: x['d7'] == (-1 if o.direction == 'LONG' else 1), None),
        ('Д2 против доминации USDT за 30 сут', 'обе', lambda o, x: x['d30'] == (-1 if o.direction == 'LONG' else 1), None),
        ('В1 F3 и мало агрессора против на откате', 'F3', lambda o, x: o.direction == 'SHORT' and x['crowd'] and x['v1'] <= th['v1'], None),
        ('В2 F3 и слабый объём отката', 'F3', lambda o, x: o.direction == 'SHORT' and x['crowd'] and x['v2'] <= th['v2'], None),
        ('Г1 F3, половина на 1R + безубыток', 'F3', lambda o, x: o.direction == 'SHORT' and x['crowd'], 'Г1'),
        ('Г2 F3, трейлинг на 1R риска', 'F3', lambda o, x: o.direction == 'SHORT' and x['crowd'], 'Г2'),
        # для понимания — те же идеи на второй базе
        ('(А1 на F3)', 'F3', lambda o, x: o.direction == 'SHORT' and x['crowd'] and x['a1'], None),
        ('(Б1 и против толпы)', 'обе', lambda o, x: x['crowd'] and side(o, x, 'b7'), None),
        ('(Д1 и против толпы)', 'обе', lambda o, x: x['crowd'] and x['d7'] == (-1 if o.direction == 'LONG' else 1), None),
        ('(В1 на обе)', 'обе', lambda o, x: x['v1'] <= th['v1'], None),
        ('(Г1 на обе)', 'обе', lambda o, x: True, 'Г1'),
    ]


def run_cell(args):
    cell, feat, th = args
    cache, orders, pairs = load_cell(cell)
    orders = [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT]
    bt.CACHE_DIR = os.path.join(HERE, cache)
    exec_data = {p: d['5m'] for p in pairs if (d := bt.load_pair(p)) is not None}
    out = {}
    for name, base, rule, exit_kind in rules(th):
        pool = [o for i, o in enumerate(orders) if i in feat.index and rule(o, feat.loc[i])]
        if exit_kind:
            pool = with_exit(pool, exit_kind)
        kw = dict(BASE, breakeven_after_tp1=(exit_kind == 'Г1'))
        for through in (0.0, 0.0005):
            smc_engine.FILL_THROUGH_PCT = through
            try:
                res = smc_engine.run_portfolio(pool, exec_data, **kw)
            finally:
                smc_engine.FILL_THROUGH_PCT = 0.0
            out[(name, through)] = np.array([t['pnl'] / t['risk'] for t in res['trades']])
    print(f'   {cell}: готово', flush=True)
    return cell, out


def mean(a):
    return float(a.mean()) if len(a) else float('nan')


def main():
    with Pool(min(len(CELLS), 5)) as pool:
        feats = dict(pool.imap_unordered(features, CELLS))
    sel = pd.concat([feats[('замер', 'bear')], feats[('замер', 'mid1')]])
    th = {'v1': float(sel.v1.median()), 'v2': float(sel.v2.median())}
    print(f'пороги В (медианы bear+mid1, 10 пар замера): v1 {th["v1"]:.3f}, v2 {th["v2"]:.3f}')
    for c in CELLS:
        f = feats[c]
        print(f'   {c}: заявок {len(f)}; А1 {f.a1.mean():.0%} А2 {f.a2.mean():.0%}; '
              f'v1 есть {f.v1.notna().mean():.0%}, v2 есть {f.v2.notna().mean():.0%}')
    with Pool(min(len(CELLS), 5)) as pool:
        res = dict(pool.imap_unordered(run_cell, [(c, feats[c], th) for c in CELLS]))

    names = [(n, b) for n, b, _, _ in rules(th)]
    print('\nR на сделку по ячейкам (касание), затем всё вместе: касание [95%] / насквозь')
    print(f'{"":46s} ' + ' '.join(f'{g[:3]}-{p:4s}' for g, p in CELLS))
    for n, _ in names:
        cells = ' '.join(f'{mean(res[c][(n, 0.0)]):+.3f}' for c in CELLS)
        a = np.concatenate([res[c][(n, 0.0)] for c in CELLS])
        t = np.concatenate([res[c][(n, 0.0005)] for c in CELLS])
        lo, hi = ci(a)
        print(f'{n:46s} {cells}\n{"":46s} сделок {len(a)}, {mean(a):+.3f} [{lo:+.3f}; {hi:+.3f}] | насквозь {mean(t):+.3f}')
    print('\nПРИЁМКА (8 из 9 ячеек лучше базы; насквозь > 0; нижняя граница > 0)')
    for n, b in names[2:12]:
        base = 'база: обе стороны' if b == 'обе' else 'база: F3 (как в боте)'
        better = sum(mean(res[c][(n, 0.0)]) > mean(res[c][(base, 0.0)]) for c in CELLS)
        a = np.concatenate([res[c][(n, 0.0)] for c in CELLS])
        t = np.concatenate([res[c][(n, 0.0005)] for c in CELLS])
        lo, _ = ci(a)
        ok = better >= 8 and mean(t) > 0 and lo > 0
        print(f'  {n:46s} лучше базы в {better}/9; насквозь {mean(t):+.3f}; нижняя {lo:+.3f} → '
              f'{"ПРИНЯТА" if ok else "не принята"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
