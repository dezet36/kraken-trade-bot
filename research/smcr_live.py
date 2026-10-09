"""
Разбор SMC 01.10.2026 (docs/SMC_разбор_аналитика_2026-10-01.md): правила
решений D1–D4 и данные биржи F1–F4 на сетапах «глазами бота»
(research/smc_lab_live.py), живые пороги research/smc_lab_eval.LIVE.

Выборки:
    pool   10 пар пула SMC, пять периодов (раскладка 'live')
    other  10 пар списка бота вне пула (раскладка 'live20' минус пул)
    x12    23 пары кэша 12m вне списка бота (раскладка 'live12x')

На каждую заявку: R базы (налив касанием и насквозь 0.05%), R с D1 (снятие
заявки при потере направления старшего ТФ), поправка фандинга по факту (F4),
признаки D3, D4, F1–F3, фильтр толпы бота (ставка в сторону сделки ≤ −1 б.п.).
D2 (стоп за началом ноги) — отдельный набор заявок: меняется стоп, R:R и
отбор по порогам.

    python research/smcr_live.py build     # → results/smcr/live_orders.pkl
"""
import os
import sys
from functools import lru_cache

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

import ai_doctrine as D                               # noqa: E402

D.PERIODS['fresh'] = 'backtest_cache_fresh20'          # все 20 пар; у 10 пула свечи те же

import smc_lab_eval as E                              # noqa: E402
import backtest_smc as bt                             # noqa: E402

OUT = os.path.join(HERE, 'results', 'smcr')
POOL10 = tuple(bt.DEFAULT_PAIRS)
FT = 0.0005
H_MS = 3_600_000
FUND_FLAT = 0.0003                                      # smc_engine.FUNDING_PCT_PER_DAY
for name in ('live', 'live20', 'live12x', 'live_D2', 'live20_D2', 'live12x_D2'):
    E.BAR_H.setdefault(name, 1.0)


# ── Данные ───────────────────────────────────────────────────────────────────
@lru_cache(maxsize=None)
def candles_1h(period, pair):
    bt.CACHE_DIR = os.path.join(HERE, D.PERIODS[period])
    return bt.load_cached(pair, '1h')


@lru_cache(maxsize=None)
def flow(pair):
    parts = []
    for f in ('flow_cache_2021', 'flow_cache', 'flow_cache_new', 'flow_cache_extra'):
        p = os.path.join(HERE, f, pair + '.pkl')
        if os.path.exists(p):
            parts.append(pd.read_pickle(p))
    if not parts:
        return None
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep='last')].sort_index()
    ms = ((df.index - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy('int64')
    return {'ms': ms, 'v': df['v'].to_numpy(float), 'tb': df['tb'].to_numpy(float),
            'oi': df['oi'].to_numpy(float), 'fund': df['funding'].to_numpy(float)}


@lru_cache(maxsize=None)
def bias_timeline(period, pair):
    """Направление старшего ТФ (день + 4 ч, как у бота) на закрытии каждого часа."""
    import logger
    logger.log = lambda *a, **k: None
    from strategies.smc import signal as smc_signal
    bt.CACHE_DIR = os.path.join(HERE, D.PERIODS[period])
    data = bt.load_pair(pair)
    if data is None:
        return None
    ctx = smc_signal.build_context({'bias': data['1d'], 'htf': data['4h'], 'poi': data['1h']}, pair=pair)
    ts = data['1h']['timestamp']
    close_ms = ((ts - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy('int64') + H_MS
    side = np.array([{'BULLISH': 1, 'BEARISH': -1}.get(ctx.bias_at(t), 0) for t in ts], dtype=int)
    return close_ms, side


def _ms_of(period, pair, idx):
    c = candles_1h(period, pair)
    if c is None or idx < 0 or idx >= len(c):
        return None
    return int(pd.Timestamp(c['timestamp'].iloc[idx]).value // 10 ** 6)


def flow_feats(period, r):
    """F1–F3 и D3 для строки срабатывания."""
    out = {'F1': np.nan, 'F2': np.nan, 'F3': np.nan, 'D3': np.nan}
    fl = flow(r.pair)
    t0 = _ms_of(period, r.pair, int(r.leg_start_i))
    t1 = _ms_of(period, r.pair, int(r.leg_end_i))
    if fl is not None and t0 is not None and t1 is not None:
        ms = fl['ms']
        a = int(np.searchsorted(ms, t0))
        b = int(np.searchsorted(ms, t1, side='right'))           # бары ноги [a, b)
        d = int(np.searchsorted(ms, int(r.t) - H_MS, side='right'))  # по бар решения включительно
        if a >= 170 and b > a and d >= b and d <= len(ms):
            v_leg = fl['v'][a:b].sum()
            v_base = fl['v'][a - 168:a].sum()
            if v_leg > 0 and v_base > 0:
                share = fl['tb'][a:b].sum() / v_leg - fl['tb'][a - 168:a].sum() / v_base
                out['F1'] = r.dir * share
            oi0, oi1, oid = fl['oi'][a - 1], fl['oi'][b - 1], fl['oi'][d - 1]
            if oi0 > 0 and oi1 > 0:
                out['F2'] = oi1 / oi0 - 1
                out['F3'] = oid / oi1 - 1
    c = candles_1h(period, r.pair)
    if c is not None:
        # индекс бара решения по времени закрытия
        ts_ms = ((c['timestamp'] - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy('int64')
        k = int(np.searchsorted(ts_ms, int(r.t) - H_MS, side='right')) - 1
        if k >= 480:
            lo = c['low'].iloc[k - 479:k + 1].min()
            hi = c['high'].iloc[k - 479:k + 1].max()
            if hi > lo:
                pos = (r.entry_near - lo) / (hi - lo)
                out['D3'] = pos if r.dir > 0 else 1 - pos            # ≤ 0.5 — «в своей половине»
    return out


def real_funding_adj(res, pair, side):
    """R-поправка: плоский расход стенда → фактические выплаты фандинга."""
    fl = flow(pair)
    if fl is None or res is None:
        return np.nan
    t0 = int(pd.Timestamp(res['entry_time']).value // 10 ** 6)
    t1 = int(pd.Timestamp(res['exit_time']).value // 10 ** 6)
    days = (t1 - t0) / 86_400_000
    flat_r = res['funding'] / res['risk']
    if days <= 0:
        return flat_r                                   # плоский расход снимается целиком
    ms = fl['ms']
    a = int(np.searchsorted(ms, t0, side='right'))
    b = int(np.searchsorted(ms, t1, side='right'))
    hours = (ms[a:b] // H_MS) % 24
    events = fl['fund'][a:b][hours % 8 == 0]
    paid = side * events.sum()                          # >0 — платим
    real_r = flat_r * paid / (FUND_FLAT * days)
    return flat_r - real_r                              # прибавка к R


# ── Заявки и исполнение ──────────────────────────────────────────────────────
def spec_for(layout, pairs):
    return dict(E.LIVE, pairs=tuple(pairs), layouts=(layout,), filt=lambda r, x: True)


def inject_d2(period, layout, new_layout):
    """Строки с D2: стоп — дальний из нынешнего и начала ноги ±0.15%."""
    f = E.rows(period, layout).copy()
    leg = f['leg_start']
    alt = np.where(f['dir'] > 0, leg * (1 - 0.0015), leg * (1 + 0.0015))
    f['stop'] = np.where(f['dir'] > 0, np.minimum(f['stop'], alt), np.maximum(f['stop'], alt))
    E._rows[(period, new_layout)] = f


def simulate(period, pairs, orders, ft=0.0, cancel=None):
    data = E.exec_data(period, pairs)
    prep = simulate.cache.setdefault(period, {})
    for p, df in data.items():
        if p not in prep:
            prep[p] = E._prepare(df)
    out = []
    E.smc_engine.FILL_THROUGH_PCT = ft
    try:
        for o in orders:
            arr = prep.get(o.pair)
            if arr is None:
                out.append(None)
                continue
            oo = o
            if cancel is not None:
                ct = cancel(o)
                if ct is not None and ct < o.expires:
                    oo = E.Order(pair=o.pair, direction=o.direction, entry=o.entry, stop=o.stop,
                                 targets=o.targets, fractions=o.fractions, created=o.created,
                                 expires=ct, key=o.key, meta=o.meta)
            start = int(np.searchsorted(arr['ts'], np.datetime64(oo.created), side='right'))
            out.append(E.simulate_order(oo, arr, start, 100.0, breakeven_after_tp1=False,
                                        max_hold_hours=336.0, cancel_at_target=False))
    finally:
        E.smc_engine.FILL_THROUGH_PCT = 0.0
    return out


simulate.cache = {}


def cancel_on_bias(period):
    def fn(o):
        tl = bias_timeline(period, o.pair)
        if tl is None:
            return None
        close_ms, side = tl
        want = 1 if o.direction == 'BULLISH' else -1
        t = int(o.created.astype('datetime64[ms]').astype('int64'))
        k = int(np.searchsorted(close_ms, t, side='right'))
        bad = np.flatnonzero(side[k:] != want)
        if not len(bad):
            return None
        return np.datetime64(int(close_ms[k + bad[0]]), 'ms')
    return fn


def build_sample(sample, layout, pairs, periods):
    recs = []
    for period in periods:
        spec = spec_for(layout, pairs)
        orders = E.orders_for(period, spec)
        base = simulate(period, pairs, orders)
        base_ft = simulate(period, pairs, orders, ft=FT)
        canc = cancel_on_bias(period)
        d1 = simulate(period, pairs, orders, cancel=canc)
        d1_ft = simulate(period, pairs, orders, ft=FT, cancel=canc)
        for o, rb, rbf, r1, r1f in zip(orders, base, base_ft, d1, d1_ft):
            row = o.meta['row']
            feats = o.meta['feats'] or {}
            side = 1 if o.direction == 'BULLISH' else -1
            rec = {'sample': sample, 'period': period, 'pair': o.pair, 'dir': side, 't': int(row.t),
                   'r': None if rb is None else rb['pnl'] / rb['risk'],
                   'r_ft': None if rbf is None else rbf['pnl'] / rbf['risk'],
                   'r_d1': None if r1 is None else r1['pnl'] / r1['risk'],
                   'r_d1_ft': None if r1f is None else r1f['pnl'] / r1f['risk'],
                   'f4_adj': real_funding_adj(rb, o.pair, side) if rb is not None else np.nan,
                   'gross_r': None if rb is None else rb['gross_pnl'] / rb['risk'],
                   'fees_r': None if rb is None else rb['fees'] / rb['risk'],
                   'flat_fund_r': None if rb is None else rb['funding'] / rb['risk'],
                   'exit': None if rb is None else rb['exit_reason'],
                   'mfe': None if rb is None else rb['mfe_r'], 'mae': None if rb is None else rb['mae_r'],
                   'hold_h': None if rb is None else (rb['exit_time'] - rb['entry_time']) / np.timedelta64(1, 'h'),
                   'wait_h': None if rb is None else (rb['entry_time'] - o.created) / np.timedelta64(1, 'h'),
                   'stop_pct': o.meta['stop_pct'], 'rr': o.meta['rr'], 'conf': float(row.confluence),
                   'fund_bp': feats.get('funding', np.nan),
                   'crowd': not (feats.get('funding', np.nan) > -1.0),
                   'brk': row.brk, 'swept': bool(row.swept), 'ote': bool(row.f_ote_zone),
                   'stop_by_sweep': bool(row.swept) and abs(row.stop - row.invalidation * (1 + 0.0015 * (-row.dir))) > 1e-9 * row.stop,
                   'sweep_unrelated': bool(row.swept) and ((row.dir > 0 and row.sweep_extreme < row.leg_start)
                                                           or (row.dir < 0 and row.sweep_extreme > row.leg_start)),
                   }
            rec.update(flow_feats(period, row))
            recs.append(rec)
        # D2 — свой набор заявок
        inject_d2(period, layout, layout + '_D2' if layout != 'live' else 'live_D2')
        spec2 = spec_for(layout + '_D2' if layout != 'live' else 'live_D2', pairs)
        orders2 = E.orders_for(period, spec2)
        d2 = simulate(period, pairs, orders2)
        d2_ft = simulate(period, pairs, orders2, ft=FT)
        for o, rb, rbf in zip(orders2, d2, d2_ft):
            feats = o.meta['feats'] or {}
            recs.append({'sample': sample + '_D2', 'period': period, 'pair': o.pair,
                         'dir': 1 if o.direction == 'BULLISH' else -1, 't': int(o.meta['row'].t),
                         'r': None if rb is None else rb['pnl'] / rb['risk'],
                         'r_ft': None if rbf is None else rbf['pnl'] / rbf['risk'],
                         'stop_pct': o.meta['stop_pct'], 'rr': o.meta['rr'],
                         'crowd': not (feats.get('funding', np.nan) > -1.0)})
        print(f'  {sample} {period}: заявок {len(orders)}, с D2 {len(orders2)}', flush=True)
        simulate.cache.pop(period, None)
        E._exec.pop(period, None)
    return recs


def build():
    os.makedirs(OUT, exist_ok=True)
    recs = []
    recs += build_sample('pool', 'live', POOL10, E.PERIODS)
    other = tuple(p for p in D.PAIRS if p not in POOL10)
    recs += build_sample('other', 'live20', other, E.PERIODS)
    x12 = tuple(sorted(set(E.rows('12m', 'live12x')['pair'])))
    recs += build_sample('x12', 'live12x', x12, ('12m',))
    frame = pd.DataFrame(recs)
    path = os.path.join(OUT, 'live_orders.pkl')
    frame.to_pickle(path + '.tmp')
    os.replace(path + '.tmp', path)
    print('→', path, len(frame))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['build']:
        build()
