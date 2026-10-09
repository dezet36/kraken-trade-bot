"""
Разбор SMC, раздел 9 (docs/SMC_разбор_аналитика_2026-10-01.md): межтаймфреймовый
SMC — снятие ликвидности старшего ТФ → слом структуры младшего ТФ → лимит от
имбаланса младшего ТФ → цель — нетронутый пул старшего ТФ. Обобщение M1
(research/smc_newmodel.py) на ликвидность 4 ч. Общий слой smc/ не меняется.

Связки (ликвидность → слом и вход): 4ч→1ч, 4ч→15м, 1ч→15м (сверка с M1).

    python research/smcr_mtf.py gen     # → results/smcr/mtf_rows.pkl (20 пар, 5 периодов)
    python research/smcr_mtf.py eval    # → печать (results/smcr/eval_mtf.txt)
"""
import itertools
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

OUT = os.path.join(HERE, 'results', 'smcr')
CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m', 'fresh': 'backtest_cache_fresh20'}
PERIODS = ('bear', 'mid1', 'mid2', '12m', 'fresh')
H = 3_600_000
DUR = {'4h': 4 * H, '1h': H, '15m': H // 4}
# связка: (ТФ ликвидности, ТФ слома и входа, ждём слом после возврата, срок заявки, удержание), часы
COMBOS = {'4ч→1ч': ('4h', '1h', 24, 24.0, 168.0),
          '4ч→15м': ('4h', '15m', 8, 12.0, 168.0),
          '1ч→15м': ('1h', '15m', 8, 12.0, 72.0)}
BUFFER = 0.0015
MIN_STOP_PCT = 0.8
COST_LIMIT = 10.0
OFFSET = 0.001
ROUND_TRIP = 0.00075
MIN_RR, MAX_RR = 2.0, 10.0


def _ms(series):
    ts = pd.to_datetime(series, utc=True)
    return ((ts - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy(dtype='int64')


def _frame(df, dur, structure, gaps, pools=None, sweeps=None):
    from analysis.smc import liquidity
    hi, lo = df['high'].to_numpy(float), df['low'].to_numpy(float)
    f = {'ts': _ms(df['timestamp']), 'dur': dur, 'high': hi, 'low': lo, 'close': df['close'].to_numpy(float),
         'ev_idx': np.array([e['index'] for e in structure['events']], dtype=int),
         'ev_dir': np.array([e['direction'] for e in structure['events']]),
         'gaps': gaps, 'g_conf': np.array([g['confirmed_at'] for g in gaps], dtype=int),
         'g_dir': np.array([g['direction'] for g in gaps]), 'sweeps': sweeps or []}
    if pools is not None:
        inf = np.iinfo(np.int64).max
        f['p_side'] = np.array([p['side'] for p in pools])
        f['p_price'] = np.array([p['price'] for p in pools], dtype=float)
        f['p_conf'] = np.array([p['confirmed_at'] for p in pools], dtype=np.int64)
        touch = np.full(len(pools), inf, dtype=np.int64)
        for n, p in enumerate(pools):
            seg = (hi[p['confirmed_at'] + 1:] >= p['price']) if p['side'] == liquidity.BSL \
                else (lo[p['confirmed_at'] + 1:] <= p['price'])
            if seg.any():
                touch[n] = p['confirmed_at'] + 1 + int(np.argmax(seg))
        f['p_touch'] = touch
    return f


def _setups(combo, L, E, ctx, h1_stamps, ts1, period, pair):
    from analysis.smc import liquidity
    _, _, confirm_h, _, _ = COMBOS[combo]
    rows = []
    for sw in L['sweeps']:
        k_p, k_r = int(sw['index']), int(sw['reclaimed_at'])
        if k_r >= len(L['ts']) - 1:
            continue
        bull = sw['direction'] == 'BULLISH'
        side = 1 if bull else -1
        t_p0 = L['ts'][k_p]
        t_r = L['ts'][k_r] + L['dur']                                 # закрытие свечи возврата
        ext = float(L['low'][k_p:k_r + 1].min()) if bull else float(L['high'][k_p:k_r + 1].max())
        j0 = int(np.searchsorted(E['ts'], t_p0))
        j_last = int(np.searchsorted(E['ts'], t_r + confirm_h * H - E['dur'], side='right')) - 1
        m = (E['ev_idx'] >= j0) & (E['ev_idx'] <= j_last) & (E['ev_dir'] == sw['direction'])
        if not m.any():
            continue
        j_ev = int(E['ev_idx'][np.argmax(m)])                         # первый слом в сторону сетапа
        gm = (E['g_conf'] >= j0) & (E['g_conf'] <= j_ev) & (E['g_dir'] == sw['direction'])
        if not gm.any():
            continue
        gap = E['gaps'][int(np.flatnonzero(gm)[-1])]
        created = max(int(E['ts'][j_ev] + E['dur']), int(t_r))
        j_c = int(np.searchsorted(E['ts'], created - E['dur'], side='right')) - 1
        price = E['close'][j_c]
        entry = gap['top'] if bull else gap['bottom']
        if (price - entry) * side <= 0:
            continue                                                  # цена уже за входом — не догоняем
        stop = ext * (1 - BUFFER) if bull else ext * (1 + BUFFER)
        if (entry - stop) * side <= 0:
            continue
        stop_pct = abs(entry - stop) / entry * 100
        if stop_pct < MIN_STOP_PCT - 1e-9:
            continue
        limit = entry * (1 + OFFSET * side)
        risk = abs(limit - stop)
        if limit / risk * ROUND_TRIP * 100 > COST_LIMIT:
            continue
        k_now = int(np.searchsorted(L['ts'], created - L['dur'], side='right')) - 1
        opp = liquidity.BSL if bull else liquidity.SSL
        ok = ((L['p_side'] == opp) & (L['p_conf'] <= k_now) & (L['p_touch'] > k_now)
              & ((L['p_price'] - limit) * side >= MIN_RR * risk))
        if not ok.any():
            continue
        target = float(L['p_price'][ok].min() if bull else L['p_price'][ok].max())
        rr = abs(target - limit) / risk
        if rr > MAX_RR:
            continue
        j1 = int(np.searchsorted(ts1, created - H, side='right')) - 1  # последний закрытый час
        bias = ctx.bias_at(h1_stamps.iloc[j1]) if j1 >= 0 else 'NEUTRAL'
        rows.append({'combo': combo, 'period': period, 'pair': pair, 'dir': side, 't': created,
                     'entry': float(limit), 'stop': float(stop), 'target': target, 'rr': float(rr),
                     'stop_pct': float(stop_pct), 'aligned': bias == sw['direction'], 'source': sw['source'],
                     'sweep': k_p})
    return rows


def _job(args):
    period, pair = args
    from infra import logger
    logger.log = lambda *a, **k: None
    import backtest_smc as bt
    from strategies.smc import signal as smc_signal
    from analysis.smc import imbalance, liquidity, structure as structure_mod
    bt.CACHE_DIR = os.path.join(HERE, CACHES[period])
    data = bt.load_pair(pair)
    if data is None:
        return period, pair, [], 0.0
    started = time.time()
    h1 = data['1h'].reset_index(drop=True)
    h4 = data['4h'].reset_index(drop=True)
    m15 = data['15m'].reset_index(drop=True)
    ctx = smc_signal.build_context({'bias': data['1d'], 'htf': h4, 'poi': h1}, pair=pair)
    s4 = structure_mod.build_structure(h4, tier='swing')
    pools4 = liquidity.find_liquidity_pools(h4, s4)
    frames = {
        '1h': _frame(h1, H, ctx.structure, ctx.fvgs, ctx.pools, ctx.sweeps),
        '4h': _frame(h4, 4 * H, s4, imbalance.find_fvg(h4), pools4, liquidity.find_sweeps(h4, pools4)),
        '15m': _frame(m15, H // 4, structure_mod.build_structure(m15, tier='swing'), imbalance.find_fvg(m15)),
    }
    ts1 = frames['1h']['ts']
    rows = []
    for combo, (lt, et, *_rest) in COMBOS.items():
        rows += _setups(combo, frames[lt], frames[et], ctx, h1['timestamp'], ts1, period, pair)
    return period, pair, rows, time.time() - started


def generate():
    import ai_doctrine as D
    os.makedirs(OUT, exist_ok=True)
    jobs = [(p, pair) for p in PERIODS for pair in D.PAIRS
            if os.path.exists(os.path.join(HERE, CACHES[p], f'{pair}_5m.pkl'))]
    out = []
    with Pool(4) as pool:
        for period, pair, rows, sec in pool.imap_unordered(_job, jobs):
            out += rows
            print(f'  {period:5s} {pair:13s} сетапов {len(rows):5d}  {sec:5.1f} с', flush=True)
    frame = pd.DataFrame(out).drop_duplicates(['combo', 'period', 'pair', 't', 'dir', 'entry', 'stop'])
    path = os.path.join(OUT, 'mtf_rows.pkl')
    frame.to_pickle(path + '.tmp')
    os.replace(path + '.tmp', path)
    print(f'→ {path}: {len(frame)} сетапов;', frame.groupby(['combo', 'period']).size().to_dict())


# ── Оценка ───────────────────────────────────────────────────────────────────
def evaluate():
    import smcr_live as L
    from common import ci
    E = L.E
    pool10 = L.POOL10
    other10 = tuple(p for p in L.D.PAIRS if p not in pool10)
    end12 = E.END_12M_MS
    frame = pd.read_pickle(os.path.join(OUT, 'mtf_rows.pkl'))
    fund_cache = {}

    def crowd_bp(period, pair, t, side):
        k = (period, pair)
        if k not in fund_cache:
            fund_cache[k] = E.S.load_series(L.D.PERIODS[period], 'funding', pair, 'funding_rate')
        got = fund_cache[k]
        if got is None:
            return np.nan
        ts, vals = got
        j = int(np.searchsorted(ts, t, side='right')) - 1
        return vals[j] * 1e4 * side if j >= 0 else np.nan

    frame['crowd_ok'] = [not (crowd_bp(r.period, r.pair, int(r.t), int(r.dir)) > -1.0) for r in frame.itertuples()]
    variants = list(itertools.product(COMBOS, ('по направлению', 'в любую'), ('без толпы', 'толпа')))
    store = {}
    for period in PERIODS:
        for sample, pairs in (('pool', pool10), ('other', other10)):
            data = E.exec_data(period, pairs)
            prep = {p: E._prepare(df) for p, df in data.items()}
            fund = E.funding_data(period, pairs)
            for v in variants:
                combo, bias, crowd = v
                _, _, _, expiry_h, hold_h = COMBOS[combo]
                f = frame[(frame['combo'] == combo) & (frame['period'] == period) & frame['pair'].isin(pairs)]
                if bias == 'по направлению':
                    f = f[f['aligned']]
                if crowd == 'толпа':
                    f = f[f['crowd_ok']]
                orders = []
                for r in f.itertuples():
                    created = np.datetime64(int(r.t), 'ms')
                    orders.append(E.Order(pair=r.pair, direction='BULLISH' if r.dir > 0 else 'BEARISH', entry=r.entry,
                                          stop=r.stop, targets=[r.target], fractions=[1.0], created=created,
                                          expires=created + np.timedelta64(int(expiry_h * 3600), 's'),
                                          key=(r.pair, combo, int(r.sweep), int(r.dir)), meta={'t': int(r.t), 'rr': r.rr}))
                orders.sort(key=lambda o: (o.created, -o.meta['rr']))
                for ft in (0.0, 0.0005):
                    E.smc_engine.FILL_THROUGH_PCT = ft
                    E.smc_engine.FUNDING_REAL = fund
                    try:
                        res = E.run_portfolio(orders, data, risk_pct=1.0, max_positions=5, cooldown_hours=12.0,
                                              breakeven_after_tp1=False, max_hold_hours=hold_h, max_same_direction=3,
                                              occupy_while_pending=True, cooldown_from_placement=True,
                                              cancel_at_target=False)
                        each = []
                        for o in orders:
                            arr = prep.get(o.pair)
                            if arr is None:
                                continue
                            start = int(np.searchsorted(arr['ts'], np.datetime64(o.created), side='right'))
                            got = E.simulate_order(o, arr, start, 100.0, breakeven_after_tp1=False, max_hold_hours=hold_h)
                            if got is not None:
                                each.append((o.meta['t'], got['pnl'] / got['risk']))
                    finally:
                        E.smc_engine.FILL_THROUGH_PCT = 0.0
                        E.smc_engine.FUNDING_REAL = None
                    port = [(t['meta']['t'], t['pnl'] / t['risk']) for t in res['trades']]
                    if period == 'fresh':
                        port = [x for x in port if x[0] > end12]
                        each = [x for x in each if x[0] > end12]
                    store[(v, sample, ft, period)] = ([x[1] for x in port], [x[1] for x in each])
            print(f'  {period} {sample}: готово', flush=True)
        E._exec.pop(period, None)
    print('\nпортфель: сделок в год, итог R по периодам (без двойного счёта); заявки по одной: R на сделку')
    for v in variants:
        def tot(group, ft=0.0, which=0, sample='pool'):
            return [x for p in group for x in store[(v, sample, ft, p)][which]]
        early_e, late_e = np.array(tot(('bear', 'mid1'), which=1)), np.array(tot(('mid2', '12m', 'fresh'), which=1))
        late_e_ft = np.array(tot(('mid2', '12m', 'fresh'), ft=0.0005, which=1))
        oth_e = np.array(tot(PERIODS, which=1, sample='other'))
        port = {p: sum(store[(v, 'pool', 0.0, p)][0]) for p in PERIODS}
        port_ft = {p: sum(store[(v, 'pool', 0.0005, p)][0]) for p in PERIODS}
        n_port = sum(len(store[(v, 'pool', 0.0, p)][0]) for p in PERIODS)
        cand = (len(early_e) and early_e.mean() > 0 and port['bear'] > 0 and port['mid1'] > 0)
        acc = (cand and len(late_e) and late_e.mean() > 0 and len(late_e_ft) and late_e_ft.mean() > 0
               and sum(port[p] > 0 for p in ('mid2', '12m', 'fresh')) >= 2
               and sum(port_ft[p] > 0 for p in ('mid2', '12m', 'fresh')) >= 2
               and len(oth_e) and oth_e.mean() > 0)
        all_e = np.concatenate([early_e, late_e]) if len(early_e) + len(late_e) else np.array([])
        lo, hi = ci(all_e) if len(all_e) > 10 else (np.nan, np.nan)
        print(f'  {v[0]:7s} | {v[1]:14s} | {v[2]:9s} | портфель {n_port:4d} сд ({n_port / 4.7:4.0f}/год) '
              + ' '.join(f'{p} {port[p]:+6.1f}' for p in PERIODS)
              + f' | заявки: отбор {early_e.mean() if len(early_e) else 0:+.3f} ({len(early_e)}), '
              f'приёмка {late_e.mean() if len(late_e) else 0:+.3f} ({len(late_e)}), насквозь '
              f'{late_e_ft.mean() if len(late_e_ft) else 0:+.3f}, вне пула {oth_e.mean() if len(oth_e) else 0:+.3f} '
              f'({len(oth_e)}); всё {all_e.mean() if len(all_e) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}] → '
              f'{"ПРИНЯТ" if acc else ("кандидат, приёмку не прошёл" if cand else "не кандидат")}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['gen']:
        generate()
    elif sys.argv[1:2] == ['eval']:
        evaluate()
