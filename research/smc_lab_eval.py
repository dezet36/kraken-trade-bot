"""
Стенд SMC, вторая половина: вариант правил → заявки → портфель с правилами
живого брокера → итог по периодам.

Вариант — словарь поверх LIVE (как SMC торгует сейчас):
    min_conf, long_premium    порог конфлюенса и надбавка для лонгов
    min_rr, max_rr            взвешенный R:R ядра
    min_stop                  минимальный стоп, % (настройка оператора: 0.8)
    cost_limit                предел издержек брокера, % риска (у SMC 10)
    offset                    смещение лимита к цене (config: 0.001)
    depth                     глубина входа в блок: 0 — ближний край, 0.5 — середина
    fill_through              налив только насквозь на эту долю цены (0 — касанием)
    fractions                 доли частичной фиксации (None — как у ядра)
    breakeven, max_hold, expiry, cancel_at_target
    cap, cooldown, occupy, cool_from_place, max_positions   правила портфеля
    pairs                     набор пар
    filt                      функция (строка срабатывания, признаки) → bool, или None

Признаки рынка на момент заявки — research/ai_setups.features (ход цены,
ATR, дневной размах, ОИ, фандинг, BTC), плюс часы до решения ФРС.

Запуск (проверка стенда на базовых вариантах):
    python research/smc_lab_eval.py
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

import logger                                          # noqa: E402
logger.log = lambda *a, **k: None

import ai_doctrine as D                               # noqa: E402
import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
import smc_engine                                     # noqa: E402
from ai_filter_study import FOMC                      # noqa: E402
from common import ci                                 # noqa: E402
from smc_engine import Order, _prepare, run_portfolio, simulate_order  # noqa: E402

OUT = os.path.join(HERE, 'results')
PERIODS = ['bear', 'mid1', 'mid2', '12m', 'fresh']
POOL10 = tuple(bt.DEFAULT_PAIRS)
ALL20 = tuple(D.PAIRS)
ROUND_TRIP = 0.00075                                   # config.ENTRY_COST_ROUND_TRIP
EVENTS_MS = np.array(sorted(int(pd.Timestamp(d + ' 18:00', tz='UTC').value // 10 ** 6) for d in FOMC))
H = 3_600_000

LIVE = dict(min_conf=4.5, long_premium=0.0, min_rr=4.0, max_rr=0.0, min_stop=0.8, cost_limit=10.0,
            offset=0.001, depth=0.0, fill_through=0.0, fractions=None, breakeven=False, max_hold=336.0,
            expiry=48.0, cancel_at_target=False, cap=3, cooldown=12.0, occupy=True, cool_from_place=True,
            max_positions=5, pairs=POOL10, filt=None, tiebreak='live')
# ПОРЯДОК ОДНОВРЕМЕННЫХ ЗАЯВОК. Сигналы нескольких пар в один час портфель
# разбирает по очереди, а мест (кэп 3 в сторону, 5 позиций) мало. Живой бот
# сортирует кандидатов цикла по конфлюенсу, затем по R:R
# (strategy_smc.scan_for_setups) — 'live'. Старые бэктесты брали их в порядке
# списка пар — 'pool' (нужен только для сверки: +141.2R на 824 сделках).
# Разница между двумя порядками на пяти периодах — десятки R: портфельный
# итог SMC чувствителен к случайной очерёдности (27.09.2026).


# ── Данные ───────────────────────────────────────────────────────────────────
_rows = {}


def rows(period):
    if period not in _rows:
        f = pd.read_pickle(os.path.join(OUT, f'smc_lab_rows_{period}.pkl'))
        f = f.sort_values(['t', 'pair']).reset_index(drop=True)
        f['key'] = list(zip(f['pair'], f['poi_type'], f['poi_index'], f['dir']))
        _rows[period] = f
    return _rows[period]


_exec = {}


def exec_data(period, pairs):
    have = _exec.setdefault(period, {})
    bt.CACHE_DIR = os.path.join(HERE, D.PERIODS[period])
    for p in pairs:
        if p not in have:
            df = bt.load_cached(p, '5m')
            have[p] = df
    return {p: have[p] for p in pairs if have.get(p) is not None}


_ctx = {}


def context(period, pair):
    """Часовые свечи, BTC, ОИ и фандинг пары — для признаков рынка."""
    key = (period, pair)
    if key not in _ctx:
        cache = D.PERIODS[period]
        _ctx[key] = (D.load(cache, pair, '1h'), D.load(cache, 'BTCUSDT', '1h'),
                     S.load_series(cache, 'open_interest', pair, 'open_interest'),
                     S.load_series(cache, 'funding', pair, 'funding_rate'))
    return _ctx[key]


@lru_cache(maxsize=None)
def features(period, pair, t, side, entry, stop, tp1):
    a1, btc, oi, fr = context(period, pair)
    if a1 is None:
        return None
    row = S.features(a1, t, 'LONG' if side > 0 else 'SHORT', entry, stop, tp1, btc, oi, fr)
    if row is None:
        return None
    k = int(np.searchsorted(EVENTS_MS, t))
    gaps = [abs(t - EVENTS_MS[j]) / H for j in (k - 1, k) if 0 <= j < len(EVENTS_MS)]
    row['fomc_hours'] = min(gaps) if gaps else np.nan
    return row


# ── Вариант → заявки ─────────────────────────────────────────────────────────
def orders_for(period, spec):
    f = rows(period)
    f = f[f['pair'].isin(spec['pairs'])]
    depth = spec['depth']
    entry = f['entry_near'] * (1 - depth) + f['entry_mid'] * depth
    sl = (entry - f['stop']).abs()
    stop_pct = sl / entry * 100
    fr_default = spec['fractions']

    def rr_of(r, e, s):
        fr = fr_default or r.fractions
        tg = r.targets[:len(fr)]
        fr = list(fr[:len(tg)])
        if not fr:
            return 0.0
        fr[-1] += 1.0 - sum(fr)
        return sum(a * abs(t - e) for a, t in zip(fr, tg)) / s

    threshold = spec['min_conf'] + np.where(f['dir'] > 0, spec['long_premium'], 0.0)
    keep = (f['confluence'] >= threshold - 1e-9) & (stop_pct >= spec['min_stop'] - 1e-9)
    f, entry, sl, stop_pct = f[keep], entry[keep], sl[keep], stop_pct[keep]
    rr = np.array([rr_of(r, e, s) for r, e, s in zip(f.itertuples(), entry, sl)])
    keep = rr >= spec['min_rr'] - 1e-9
    if spec['max_rr']:
        keep &= rr <= spec['max_rr']
    f, entry, sl, stop_pct, rr = f[keep], entry[keep], sl[keep], stop_pct[keep], rr[keep]
    out, seen = [], set()
    for r, e, s, sp, q in zip(f.itertuples(), entry, sl, stop_pct, rr):
        if r.key in seen:
            continue
        feats = None
        if spec['filt'] is not None:
            feats = features(period, r.pair, int(r.t), int(r.dir), float(e), float(r.stop), float(r.targets[0]))
            if feats is None or not spec['filt'](r, feats):
                continue
        seen.add(r.key)
        limit = e * (1 + spec['offset'] * r.dir)
        share = limit / abs(limit - r.stop) * ROUND_TRIP * 100
        if spec['cost_limit'] and share > spec['cost_limit']:
            continue                                   # брокер: «вход слишком дорог»
        fr = list(spec['fractions'] or r.fractions)
        tg = list(r.targets[:len(fr)])
        fr = fr[:len(tg)]
        fr[-1] += 1.0 - sum(fr)
        created = np.datetime64(int(r.t), 'ms')
        out.append(Order(pair=r.pair, direction='BULLISH' if r.dir > 0 else 'BEARISH', entry=float(limit),
                         stop=float(r.stop), targets=tg, fractions=fr, created=created,
                         expires=created + np.timedelta64(int(spec['expiry'] * 3600), 's'), key=r.key,
                         meta={'row': r, 'stop_pct': float(sp), 'rr': float(q), 'feats': feats}))
    # run_portfolio сортирует по времени устойчиво — порядок равных задаём здесь.
    if spec['tiebreak'] == 'pool':
        rank = {p: n for n, p in enumerate(spec['pairs'])}
        out.sort(key=lambda o: (o.created, rank.get(o.pair, 99)))
    else:
        out.sort(key=lambda o: (o.created, -o.meta['row'].confluence, -o.meta['rr']))
    return out


def portfolio(period, spec, orders=None):
    orders = orders_for(period, spec) if orders is None else orders
    data = exec_data(period, spec['pairs'])
    smc_engine.FILL_THROUGH_PCT = spec['fill_through']
    try:
        res = run_portfolio(orders, data, risk_pct=1.0, max_positions=spec['max_positions'],
                            cooldown_hours=spec['cooldown'], breakeven_after_tp1=spec['breakeven'],
                            max_hold_hours=spec['max_hold'], max_same_direction=spec['cap'],
                            occupy_while_pending=spec['occupy'], cooldown_from_placement=spec['cool_from_place'],
                            cancel_at_target=spec['cancel_at_target'])
    finally:
        smc_engine.FILL_THROUGH_PCT = 0.0
    return res, orders


def per_setup(period, spec, orders=None):
    """Каждая заявка исполнена отдельно, без портфеля: R по заявкам."""
    orders = orders_for(period, spec) if orders is None else orders
    data = exec_data(period, spec['pairs'])
    prepared = {p: _prepare(df) for p, df in data.items()}
    smc_engine.FILL_THROUGH_PCT = spec['fill_through']
    out = []
    try:
        for o in orders:
            arr = prepared.get(o.pair)
            if arr is None:
                continue
            start = int(np.searchsorted(arr['ts'], np.datetime64(o.created), side='right'))
            res = simulate_order(o, arr, start, 100.0, breakeven_after_tp1=spec['breakeven'],
                                 max_hold_hours=spec['max_hold'], cancel_at_target=spec['cancel_at_target'])
            out.append((o, None if res is None else res['pnl'] / res['risk']))
    finally:
        smc_engine.FILL_THROUGH_PCT = 0.0
    return out


def run(spec_changes=None, label='', periods=PERIODS, quiet=False):
    spec = dict(LIVE, **(spec_changes or {}))
    per, allr = {}, []
    for period in periods:
        res, orders = portfolio(period, spec)
        rs = [t['pnl'] / t['risk'] for t in res['trades']]
        per[period] = (len(rs), float(np.sum(rs)) if rs else 0.0)
        allr += rs
    r = np.array(allr)
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    if not quiet:
        print(f'  {label:44s} {len(r):5d} сд {r.sum():+8.1f}R {r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}]  '
              + ' '.join(f'{p} {per[p][1]:+6.1f}' for p in periods), flush=True)
    return {'n': len(r), 'sum': float(r.sum()), 'mean': float(r.mean()) if len(r) else 0.0, 'ci': (lo, hi),
            'per': per, 'r': r}


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    print('проверка стенда: итог по пяти периодам, R портфеля с живыми правилами')
    pool_order = {'tiebreak': 'pool', 'pairs': tuple(__import__('llm_rules').POOL)}
    run(dict(pool_order, min_stop=0.5, cost_limit=0.0, offset=0.0), 'как старый бэктест (сверка: +141.2R, 824 сд)')
    run(dict(pool_order, min_stop=0.5, offset=0.0), 'стоп от 0.5%, предел 10% (сверка: −0.2R, 716 сд)')
    print('живой порядок одновременных заявок (конфлюенс, затем R:R):')
    run({'min_stop': 0.5, 'cost_limit': 0.0, 'offset': 0.0}, 'как старый бэктест')
    run({}, 'ЖИВАЯ SMC: стоп от 0.8%, предел 10%, смещение 0.1%')
    run({'fill_through': 0.0005}, 'живая, налив насквозь 0.05%')
