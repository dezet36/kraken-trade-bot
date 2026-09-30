"""
SMC, новая модель сетапа (30.09.2026).

Владелец: «да» — собрать другую модель сетапа SMC и проверить тем же
протоколом (docs/SMC_без_фандинга_2026-09-30.md, раздел 4): нынешнее ядро
(ордер-блок, цели Фибо, R:R от 4) преимущества не показало ни само, ни с
22 улучшениями.

ПРАВИЛА И ПРИЁМКА — ЗАПИСАНЫ ДО ЗАМЕРА

M1 «снятие → слом → имбаланс» (модель входа от снятия ликвидности):
  1. Часовой график: пулы ликвидности ядра (свинги, EQH/EQL, PDH/PDL,
     PWH/PWL, PMH/PML) и их снятия — прокол с возвратом за уровень
     (smc.liquidity.find_sweeps, пороги ядра). Снятие SSL (снизу) — лонг,
     снятие BSL (сверху) — шорт.
  2. Подтверждение: на 15 минутах слом структуры (BOS или CHoCH) в сторону
     сетапа — не раньше часа прокола и не позже 8 ч после возврата.
  3. Вход: лимит на ближнем крае последнего 15-минутного имбаланса в сторону
     сетапа, появившегося между началом часа прокола и сломом (это смещение,
     которое сломало структуру). В момент постановки цена должна стоять по
     нашу сторону входа — ждём отката, не догоняем.
  4. Стоп: за экстремумом прокола +0.15% (SL_BUFFER ядра). Стоп от 0.8%,
     предел издержек 10% риска, смещение лимита 0.1% — как у живой SMC.
  5. Цель: ближайший нетронутый пул противоположной стороны на часовом
     графике не ближе 2R (одна цель, 100%); если такой дальше 10R — сетап не
     берётся. Нетронутый — цена не доходила до него с его появления.
  6. Заявка живёт 12 ч; удержание до 72 ч; пауза по паре 12 ч с постановки,
     кэп 3 в сторону — правила портфеля живой SMC.
  M1a — только по направлению старшего ТФ (bias ядра: 1D и 4H согласны);
  M1b — в любую сторону.
  Приёмка модели целиком: КАНДИДАТ — на отборе (bear + mid1, 20 пар) R на
  сделку > 0 и портфель в плюсе в обоих периодах; ПРИНЯТА — на приёмке
  (mid2 + 12m + fresh) R на сделку > 0 и портфель в плюсе минимум в двух
  периодах из трёх — касанием и насквозь, — и на 23 новых парах R на
  сделку > 0.

M2 «блок от смещения» (фильтр нынешнего ядра): среди трёх часовых свечей
  после свечи блока есть тело ≥ 1.5 ATR(14). Приёмка — как у фильтров
  research/smc_improve.py (плацебо, поправка, насквозь, 23 пары).
M3 «цель — внутренняя ликвидность» (выход нынешнего ядра): 50% на конце
  импульсной ноги (точка B), 50% на первой цели ядра; если B ближе 1R —
  выход ядра без изменений. Приёмка — как у выходов smc_improve (парная
  разница, граница с поправкой, насквозь, 23 пары).

Запуск (после smc_lab_live.py gen20 / gen12x):
    python research/smc_newmodel.py > research/results/smc_newmodel.txt
"""
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

H = 3_600_000
M15 = 900_000
FT = 0.0005
EARLY = ('bear', 'mid1')
LATE = ('mid2', '12m', 'fresh')
CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m', 'fresh': 'backtest_cache_fresh20', '12mx': 'backtest_cache_12mx'}
MIN_STOP_PCT = 0.8
COST_LIMIT = 10.0
OFFSET = 0.001
BUFFER = 0.0015
MIN_RR, MAX_RR = 2.0, 10.0
CONFIRM_HOURS = 8
EXPIRY_H = 12.0
HOLD_H = 72.0
ROUND_TRIP = 0.00075


def _ms(series):
    ts = pd.to_datetime(series, utc=True)
    return ((ts - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy(dtype='int64')


# ── M1: сетапы ───────────────────────────────────────────────────────────────
def _job(args):
    period, cache, pair = args
    import logger
    logger.log = lambda *a, **k: None
    import backtest_smc as bt
    from smc import imbalance, liquidity
    from smc import signal as smc_signal
    from smc import structure as structure_mod
    bt.CACHE_DIR = os.path.join(HERE, cache)
    data = bt.load_pair(pair)
    if data is None:
        return period, pair, []
    h1 = data['1h'].reset_index(drop=True)
    m15 = data['15m'].reset_index(drop=True)
    ctx = smc_signal.build_context({'bias': data['1d'], 'htf': data['4h'], 'poi': h1}, pair=pair)
    pools, sweeps = ctx.pools, ctx.sweeps
    ts1 = _ms(h1['timestamp'])
    hi1, lo1 = h1['high'].to_numpy(float), h1['low'].to_numpy(float)
    ts15 = _ms(m15['timestamp'])
    close15 = m15['close'].to_numpy(float)
    s15 = structure_mod.build_structure(m15, tier='swing')
    ev_idx = np.array([e['index'] for e in s15['events']], dtype=int)
    ev_dir = np.array([e['direction'] for e in s15['events']])
    gaps = imbalance.find_fvg(m15)
    g_conf = np.array([g['confirmed_at'] for g in gaps], dtype=int)
    g_dir = np.array([g['direction'] for g in gaps])
    # Пулы-цели: сторона, цена, свеча появления и ПЕРВОЕ касание ценой после
    # появления — пул «нетронут» к свече k, пока появился до k и не тронут до k.
    INF = np.iinfo(np.int64).max
    p_side = np.array([pl['side'] for pl in pools])
    p_price = np.array([pl['price'] for pl in pools], dtype=float)
    p_conf = np.array([pl['confirmed_at'] for pl in pools], dtype=np.int64)
    p_touch = np.full(len(pools), INF, dtype=np.int64)
    for n, pl in enumerate(pools):
        seg = hi1[pl['confirmed_at'] + 1:] >= pl['price'] if pl['side'] == liquidity.BSL             else lo1[pl['confirmed_at'] + 1:] <= pl['price']
        if seg.any():
            p_touch[n] = pl['confirmed_at'] + 1 + int(np.argmax(seg))
    rows = []
    for sw in sweeps:
        k_p, k_r = int(sw['index']), int(sw['reclaimed_at'])
        if k_r >= len(ts1) - 1:
            continue
        bull = sw['direction'] == 'BULLISH'
        side = 1 if bull else -1
        t_p0 = ts1[k_p]                                   # начало часа прокола
        t_r = ts1[k_r] + H                                # закрытие часа возврата
        j0 = int(np.searchsorted(ts15, t_p0))
        j_last = int(np.searchsorted(ts15, t_r + CONFIRM_HOURS * H - M15, side='right')) - 1
        m = (ev_idx >= j0) & (ev_idx <= j_last) & (ev_dir == sw['direction'])
        if not m.any():
            continue
        j_ev = int(ev_idx[np.argmax(m)])                  # первый слом в сторону сетапа
        gm = (g_conf >= j0) & (g_conf <= j_ev) & (g_dir == sw['direction'])
        if not gm.any():
            continue
        gap = gaps[int(np.flatnonzero(gm)[-1])]
        created = max(ts15[j_ev] + M15, t_r)
        j_c = int(np.searchsorted(ts15, created - M15, side='right')) - 1
        price = close15[j_c]
        entry = gap['top'] if bull else gap['bottom']
        if (price - entry) * side <= 0:
            continue                                      # цена уже за входом — не догоняем
        stop = sw['extreme'] * (1 - BUFFER) if bull else sw['extreme'] * (1 + BUFFER)
        if (entry - stop) * side <= 0:
            continue
        stop_pct = abs(entry - stop) / entry * 100
        if stop_pct < MIN_STOP_PCT - 1e-9:
            continue
        limit = entry * (1 + OFFSET * side)
        risk = abs(limit - stop)
        if limit / risk * ROUND_TRIP * 100 > COST_LIMIT:
            continue
        k_now = int(np.searchsorted(ts1, created - H, side='right')) - 1   # последний закрытый час
        opp = liquidity.BSL if bull else liquidity.SSL
        ok = ((p_side == opp) & (p_conf <= k_now) & (p_touch > k_now)
              & ((p_price - limit) * side >= MIN_RR * risk))
        if not ok.any():
            continue
        best = float(p_price[ok].min() if bull else p_price[ok].max())
        rr = abs(best - limit) / risk
        if rr > MAX_RR:
            continue
        bias = ctx.bias_at(h1['timestamp'].iloc[k_r])
        rows.append({'period': period, 'pair': pair, 'dir': side, 't': int(created), 'entry': float(limit),
                     'stop': float(stop), 'target': float(best), 'rr': float(rr), 'stop_pct': float(stop_pct),
                     'aligned': bias == sw['direction'], 'source': sw['source'],
                     'penetration': float(sw['penetration_pct']), 'sweep': k_p,
                     'confirm_h': (ts15[j_ev] + M15 - t_p0) / H})
    return period, pair, rows


def generate(jobs):
    out = []
    with Pool(4) as pool:
        for period, pair, rows in pool.imap_unordered(_job, jobs):
            out += rows
    frame = pd.DataFrame(out)
    # Одна свеча может проколоть сразу несколько пулов на одном уровне (свинг и
    # EQL, PDL): это один сетап, а не несколько.
    return frame.drop_duplicates(['period', 'pair', 't', 'dir', 'entry', 'stop']).reset_index(drop=True)


# ── Исполнение ───────────────────────────────────────────────────────────────
def orders(frame):
    import smc_lab_eval as E
    out = []
    for r in frame.itertuples():
        created = np.datetime64(int(r.t), 'ms')
        out.append(E.Order(pair=r.pair, direction='BULLISH' if r.dir > 0 else 'BEARISH', entry=r.entry, stop=r.stop,
                           targets=[r.target], fractions=[1.0], created=created,
                           expires=created + np.timedelta64(int(EXPIRY_H * 3600), 's'),
                           key=(r.pair, 'SWEEP', int(r.sweep), int(r.dir)), meta={'rr': r.rr}))
    out.sort(key=lambda o: (o.created, -o.meta['rr']))
    return out


def per_setup(frame, cache, ft=0.0):
    import backtest_smc as bt
    import smc_lab_eval as E
    bt.CACHE_DIR = os.path.join(HERE, cache)
    res = []
    prepared = {}
    E.smc_engine.FILL_THROUGH_PCT = ft
    try:
        for o in orders(frame):
            if o.pair not in prepared:
                df = bt.load_cached(o.pair, '5m')
                prepared[o.pair] = E._prepare(df) if df is not None else None
            arr = prepared[o.pair]
            if arr is None:
                res.append(None)
                continue
            start = int(np.searchsorted(arr['ts'], np.datetime64(o.created), side='right'))
            got = E.simulate_order(o, arr, start, 100.0, breakeven_after_tp1=False, max_hold_hours=HOLD_H)
            res.append(None if got is None else got['pnl'] / got['risk'])
    finally:
        E.smc_engine.FILL_THROUGH_PCT = 0.0
    return np.array([x for x in res if x is not None], dtype=float)


def portfolio(frame, cache, ft=0.0):
    import backtest_smc as bt
    import smc_lab_eval as E
    bt.CACHE_DIR = os.path.join(HERE, cache)
    pairs = sorted(set(frame['pair']))
    data = {p: bt.load_cached(p, '5m') for p in pairs}
    data = {p: d for p, d in data.items() if d is not None}
    E.smc_engine.FILL_THROUGH_PCT = ft
    try:
        res = E.run_portfolio(orders(frame), data, risk_pct=1.0, max_positions=5, cooldown_hours=12.0,
                              breakeven_after_tp1=False, max_hold_hours=HOLD_H, max_same_direction=3,
                              occupy_while_pending=True, cooldown_from_placement=True, cancel_at_target=False)
    finally:
        E.smc_engine.FILL_THROUGH_PCT = 0.0
    return np.array([t['pnl'] / t['risk'] for t in res['trades']], dtype=float)


def fmt(r):
    from common import ci
    if len(r) < 10:
        return f'{len(r):4d} сд {r.sum() if len(r) else 0:+7.1f}R'
    lo, hi = ci(r)
    return f'{len(r):4d} сд, в плюс {np.mean(r > 0):4.0%}, {r.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}], итог {r.sum():+7.1f}R'


def m1(frames, label, keep):
    print(f'\n{label}')
    verdict = {}
    for group, periods in (('отбор', EARLY), ('приёмка', LATE)):
        allr, allr_ft, per, per_ft = [], [], {}, {}
        for period in periods:
            f = frames[period][keep(frames[period])]
            r = per_setup(f, CACHES[period])
            p = portfolio(f, CACHES[period])
            p_ft = portfolio(f, CACHES[period], FT)
            allr += list(r)
            allr_ft += list(per_setup(f, CACHES[period], FT))
            per[period], per_ft[period] = p.sum(), p_ft.sum()
            print(f'  {period:6s} заявки: {fmt(r)} | портфель {len(p):4d} сд {p.sum():+7.1f}R, насквозь {p_ft.sum():+7.1f}R')
        r, r_ft = np.array(allr), np.array(allr_ft)
        print(f'  {group.upper():8s} заявки: {fmt(r)}; насквозь {r_ft.mean() if len(r_ft) else 0:+.3f}R')
        verdict[group] = (r.mean() if len(r) else 0.0, r_ft.mean() if len(r_ft) else 0.0, per, per_ft)
    fx = frames['12mx'][keep(frames['12mx'])]
    rx = per_setup(fx, CACHES['12mx'])
    px = portfolio(fx, CACHES['12mx'])
    print(f'  23 НОВЫЕ ПАРЫ заявки: {fmt(rx)} | портфель {len(px)} сд {px.sum():+.1f}R')
    e_mean, _, e_per, _ = verdict['отбор']
    l_mean, l_ft, l_per, l_per_ft = verdict['приёмка']
    cand = e_mean > 0 and all(e_per[p] > 0 for p in EARLY)
    acc = (cand and l_mean > 0 and l_ft > 0 and sum(l_per[p] > 0 for p in LATE) >= 2
           and sum(l_per_ft[p] > 0 for p in LATE) >= 2 and len(rx) and rx.mean() > 0)
    print(f'  → {"ПРИНЯТА" if acc else ("кандидат, но приёмку не прошла" if cand else "не кандидат")}')
    return acc


def main():
    import ai_doctrine as D
    periods = list(EARLY) + list(LATE)
    jobs = []
    for period in periods:
        cache = CACHES[period]
        for pair in D.PAIRS:
            if os.path.exists(os.path.join(HERE, cache, f'{pair}_5m.pkl')):
                jobs.append((period, cache, pair))
    x_pairs = open(os.path.join(HERE, CACHES['12mx'], 'PAIRS.txt')).read().split()
    jobs += [('12mx', CACHES['12mx'], p) for p in x_pairs]
    frame = generate(jobs)
    frame.to_pickle(os.path.join(HERE, 'results', 'smc_newmodel_setups.pkl'))
    frames = {p: frame[frame['period'] == p] for p in periods + ['12mx']}
    print('M1 «снятие → слом → имбаланс»: сетапов по периодам ' +
          ', '.join(f'{p} {len(frames[p])}' for p in periods + ['12mx']))
    m1(frames, 'M1a — по направлению старшего ТФ', lambda f: f['aligned'])
    m1(frames, 'M1b — в любую сторону', lambda f: f['aligned'] | ~f['aligned'])
    m23()


# ── M2, M3: модификации нынешнего ядра ───────────────────────────────────────
_ARR = {}


def _h1(period, pair):
    """Часовые свечи пары: тела и ATR(14) — индексы как у строк стенда (сквозные)."""
    key = (period, pair)
    if key not in _ARR:
        import ai_doctrine as D
        import backtest_smc as bt
        bt.CACHE_DIR = os.path.join(HERE, D.PERIODS[period])
        df = bt.load_cached(pair, '1h')
        if df is None:
            _ARR[key] = None
        else:
            o, h, l, c = (df[k].to_numpy(float) for k in ('open', 'high', 'low', 'close'))
            prev = np.concatenate([[c[0]], c[:-1]])
            tr = np.maximum(h - l, np.maximum(abs(h - prev), abs(l - prev)))
            atr = pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()
            _ARR[key] = (abs(c - o), atr)
    return _ARR[key]


def displacement(row):
    got = _h1(row.period, row.pair)
    if got is None:
        return np.nan
    body, atr = got
    i = int(row.poi_index)
    if i + 3 >= len(body) or not np.isfinite(atr[i]) or atr[i] <= 0:
        return np.nan
    return float(body[i + 1:i + 4].max() / atr[i])


def m3_exit(o):
    import smc_improve as I
    row = o.meta['row']
    side = 1 if o.direction == 'BULLISH' else -1
    b = float(row.leg_end)
    if (b - o.entry) * side < abs(o.entry - o.stop) or not o.targets:
        return o
    return I._clone(o, targets=[b, o.targets[0]], fractions=[0.5, 0.5])


def m23():
    import smc_improve as I
    I.FILTERS = {'M2 displacement': lambda r, x: displacement(r) >= 1.5}
    I.EXITS = {'M3 internal': (m3_exit, {})}
    E = I.E
    pairs20 = tuple(I.D.PAIRS)
    full = I.build(E.PERIODS, 'live20', pairs20)
    x12 = I.build(('12m',), 'live12x', tuple(sorted(set(E.rows('12m', 'live12x')['pair']))))
    early, late = full[full['period'].isin(EARLY)], full[full['period'].isin(LATE)]
    print()
    print('M2 «блок от смещения» (фильтр нынешнего ядра, заявки «глазами бота»)')
    name = 'M2 displacement'
    eff, p, n = I.placebo(early, early[name])
    print(f'  отбор: {n} сд, эффект {eff:+.3f}, p {p:.3f}')
    if np.isfinite(eff) and eff > 0 and p < 0.10:
        eff_l, p_l, n_l = I.placebo(late, late[name])
        eff_ft, _, _ = I.placebo(late, late[name], col='r_ft')
        eff_x, p_x, n_x = I.placebo(x12, x12[name])
        ok = eff_l > 0 and p_l < 0.05 and eff_ft > 0 and eff_x > 0
        print(f'  приёмка: {n_l} сд, эффект {eff_l:+.3f}, p {p_l:.4f}, насквозь {eff_ft:+.3f}; '
              f'23 пары: {n_x} сд, эффект {eff_x:+.3f}  → {"ПРИНЯТ" if ok else "нет"}')
    else:
        print('  → не кандидат')
    print()
    print('M3 «цель — внутренняя ликвидность» (выход нынешнего ядра, те же заявки)')
    name = 'M3 internal'
    d, boot, n = I.paired(early, name)
    print(f'  отбор: {n} сд, разница {d:+.3f} [{np.percentile(boot, 2.5):+.3f}; {np.percentile(boot, 97.5):+.3f}]')
    if np.isfinite(d) and d > 0:
        d_l, boot_l, n_l = I.paired(late, name)
        d_ft, _, _ = I.paired(late, name + ' ft', col_base='r_ft')
        d_x, _, _ = I.paired(x12, name)
        lo = np.percentile(boot_l, 2.5)
        ok = lo > 0 and d_ft > 0 and d_x > 0
        print(f'  приёмка: {n_l} сд, разница {d_l:+.3f}, нижняя граница {lo:+.3f}, насквозь {d_ft:+.3f}; '
              f'23 пары {d_x:+.3f}  → {"ПРИНЯТ" if ok else "нет"}')
    else:
        print('  → не кандидат')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
