"""
Поглощение ликвидности на снятиях (docs/Поглощение_на_снятиях_2026-10-01.md):
все снятия общего слоя на часе → сделка на каждое → признаки потока тейкеров
и ОИ (метрики Binance, 5 мин) и стакана (bookDepth Binance) → гипотезы L1–L4.
Файлы бота не меняются.

    python research/smcr_absorb.py gen    # → results/smcr/absorb_rows_<период>.pkl (20 пар, 5 периодов)
    python research/smcr_absorb.py sim    # → results/smcr/absorb_trades.pkl (R каждой сделки)
    python research/smcr_absorb.py eval   # признаки + гипотезы → печать (results/smcr/eval_absorb.txt)
"""
import os
import sys
import time
from functools import lru_cache
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

OUT = os.path.join(HERE, 'results', 'smcr')
MET = os.path.join(HERE, 'binance_metrics_cache')
BOOK = os.path.join(HERE, 'binance_bookdepth_cache')
CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m', 'fresh': 'backtest_cache_fresh20'}
PERIODS = ('bear', 'mid1', 'mid2', '12m', 'fresh')
H_MS = 3_600_000
M5_MS = 300_000
BUFFER = 0.0015                                         # стоп за экстремумом снятия
MIN_STOP = 0.008                                        # теснее — событие не берётся
TARGET_R = 2.0
MAX_HOLD_H = 48.0
WIN_H = 168                                             # нормировка признаков — прошлые 7 суток
WIN_5M = 2016
BOOK_FROM_MS = int(pd.Timestamp('2023-01-01', tz='UTC').value // 10 ** 6)
EPOCH = pd.Timestamp('1970-01-01', tz='UTC')


def _ms(index):
    return ((pd.DatetimeIndex(index) - EPOCH) // pd.Timedelta(milliseconds=1)).to_numpy('int64')


# ── События ──────────────────────────────────────────────────────────────────
def _job(args):
    period, pair = args
    from infra import logger
    logger.log = lambda *a, **k: None
    import backtest_smc as bt
    from strategies.smc import signal as smc_signal
    bt.CACHE_DIR = os.path.join(HERE, CACHES[period])
    df_1h = bt.load_cached(pair, '1h')
    if df_1h is None or not os.path.exists(os.path.join(bt.CACHE_DIR, f'{pair}_5m.pkl')):
        return period, pair, [], 0.0
    started = time.time()
    df_4h = bt.load_cached(pair, '4h')
    if df_4h is None:
        df_4h = bt.resample(df_1h, '4h')
    ctx = smc_signal.build_context({'bias': bt.resample(df_1h, '1D'), 'htf': df_4h, 'poi': df_1h}, pair=pair)
    high = df_1h['high'].to_numpy(float)
    low = df_1h['low'].to_numpy(float)
    close = df_1h['close'].to_numpy(float)
    stamps = df_1h['timestamp']
    t_open = _ms(pd.to_datetime(stamps, utc=True))
    bias_of = {}
    best = {}
    for s in ctx.sweeps:
        i, a = int(s['reclaimed_at']), int(s['index'])
        if i < 60 or i >= len(df_1h):
            continue
        side = 1 if s['direction'] == 'BULLISH' else -1
        key = (i, side)
        cur = best.get(key)
        if cur is not None and s['weight'] <= cur['weight']:
            cur['n_pools'] += 1
            continue
        if i not in bias_of:
            bias_of[i] = ctx.bias_at(stamps.iloc[i])
        bias = bias_of[i]
        trend = ('with' if bias == s['direction'] else
                 'against' if bias in ('BULLISH', 'BEARISH') else 'none')
        ext = float(low[a:i + 1].min()) if side > 0 else float(high[a:i + 1].max())
        best[key] = {'period': period, 'pair': pair, 'a': a, 'i': i, 'dir': side, 'trend': trend,
                     'ta': int(t_open[a]), 't': int(t_open[i]) + H_MS,
                     'level': float(s['level']), 'extreme': ext, 'weight': float(s['weight']),
                     'source': s['source'], 'close': float(close[i]),
                     'pen': abs(ext - float(s['level'])) / float(s['level']),
                     'n_pools': 1 if cur is None else cur['n_pools'] + 1}
    return period, pair, list(best.values()), time.time() - started


def rows_path(period):
    return os.path.join(OUT, f'absorb_rows_{period}.pkl')


def generate():
    import ai_doctrine as D
    os.makedirs(OUT, exist_ok=True)
    jobs = [(p, pair) for p in PERIODS for pair in D.PAIRS if not os.path.exists(rows_path(p))]
    acc = {}
    with Pool(4) as pool:
        for period, pair, rows, sec in pool.imap_unordered(_job, jobs):
            acc.setdefault(period, []).extend(rows)
            print(f'  {period:5s} {pair:13s} снятий {len(rows):5d}  {sec:5.1f} с', flush=True)
    for period, rows in acc.items():
        path = rows_path(period)
        pd.DataFrame(rows).to_pickle(path + '.tmp')
        os.replace(path + '.tmp', path)
        print(f'{period}: {len(rows)} → {path}', flush=True)


# ── Сделки ───────────────────────────────────────────────────────────────────
def simulate():
    import smcr_live as L
    E = L.E
    pool10 = set(L.POOL10)
    out = []
    for period in PERIODS:
        rows = pd.read_pickle(rows_path(period))
        pairs = sorted(rows['pair'].unique())
        data = E.exec_data(period, pairs)
        E.smc_engine.FUNDING_REAL = E.funding_data(period, pairs)
        started = time.time()
        try:
            for pair, g in rows.groupby('pair'):
                if pair not in data:
                    continue
                arr = E._prepare(data[pair])
                for r in g.itertuples():
                    created = np.datetime64(int(r.t) - 1, 'ms')
                    k = int(np.searchsorted(arr['ts'], created, side='right'))
                    if k >= len(arr['ts']):
                        continue
                    side = int(r.dir)
                    entry = float(arr['open'][k])
                    stop = r.extreme * (1 - BUFFER) if side > 0 else r.extreme * (1 + BUFFER)
                    risk = (entry - stop) * side
                    if risk <= 0 or risk / entry < MIN_STOP:
                        continue
                    o = E.Order(pair=pair, direction='BULLISH' if side > 0 else 'BEARISH', entry=entry,
                                stop=float(stop), targets=[entry + side * TARGET_R * risk], fractions=[1.0],
                                created=created, expires=created + np.timedelta64(3600, 's'),
                                key=(pair, int(r.i), side), entry_type='stop')
                    res = E.simulate_order(o, arr, k, 100.0, breakeven_after_tp1=False,
                                           max_hold_hours=MAX_HOLD_H)
                    if res is None:
                        continue
                    rec = r._asdict()
                    rec.pop('Index', None)
                    rec.update({'sample': 'pool' if pair in pool10 else 'other',
                                'r': res['pnl'] / res['risk'], 'exit': res['exit_reason'],
                                'stop_pct': risk / entry, 'mfe': res['mfe_r'], 'mae': res['mae_r']})
                    out.append(rec)
        finally:
            E.smc_engine.FUNDING_REAL = None
        E._exec.pop(period, None)
        print(f'  {period}: сделок {sum(1 for x in out if x["period"] == period)} '
              f'из {len(rows)} снятий, {time.time() - started:.0f} с', flush=True)
    f = pd.DataFrame(out)
    path = os.path.join(OUT, 'absorb_trades.pkl')
    f.to_pickle(path + '.tmp')
    os.replace(path + '.tmp', path)
    print(f'сделок {len(f)} → {path}', flush=True)


# ── Признаки ─────────────────────────────────────────────────────────────────
def _z(s, window, min_periods):
    m = s.rolling(window, min_periods=min_periods).mean().shift(1)
    sd = s.rolling(window, min_periods=min_periods).std().shift(1)
    return (s - m) / sd


@lru_cache(maxsize=None)
def flow_hourly(pair):
    """Почасовые z-оценки: TK — среднее ln(покупки/продажи тейкеров) за час,
    OI — изменение ln(ОИ) за час. Метка — начало часа; час известен к его концу."""
    p = os.path.join(MET, pair + '.pkl')
    if not os.path.exists(p):
        return None
    f = pd.read_pickle(p)
    ratio = f['sum_taker_long_short_vol_ratio']
    lr = np.log(ratio.where(ratio > 0))
    g = lr.resample('1h')
    tk = g.mean().where(g.count() >= 6)
    oi = f['sum_open_interest'].where(f['sum_open_interest'] > 0)
    oi_last = np.log(oi.resample('1h').last())
    full = pd.date_range(tk.index[0], tk.index[-1], freq='1h')
    tk = tk.reindex(full)
    doi = oi_last.reindex(full).diff()
    out = pd.DataFrame({'tkz': _z(tk, WIN_H, WIN_H // 2), 'oiz': _z(doi, WIN_H, WIN_H // 2)}, index=full)
    return _ms(out.index), out['tkz'].to_numpy(float), out['oiz'].to_numpy(float)


@lru_cache(maxsize=None)
def book(pair):
    """Стакан: глубина 1% каждой стороны к своей медиане за 7 суток и z перевеса
    ln(покупки 1% / продажи 1%). Метка — начало пятиминутки, известно к её концу."""
    p = os.path.join(BOOK, pair + '.pkl')
    if not os.path.exists(p):
        return None
    f = pd.read_pickle(p).astype(float)
    full = pd.date_range(f.index[0], f.index[-1], freq='5min')
    f = f.reindex(full)
    b1 = f['b1'].where(f['b1'] > 0)
    a1 = f['a1'].where(f['a1'] > 0)
    wall_b = np.log(b1 / b1.rolling(WIN_5M, min_periods=WIN_5M // 2).median().shift(1))
    wall_a = np.log(a1 / a1.rolling(WIN_5M, min_periods=WIN_5M // 2).median().shift(1))
    imbz = _z(np.log(b1 / a1), WIN_5M, WIN_5M // 2)
    return _ms(full), wall_b.to_numpy(float), wall_a.to_numpy(float), imbz.to_numpy(float)


def _row_at(ms, t_known, max_age_ms):
    """Последняя строка, известная к моменту t_known (метка ≤ t_known − 5 мин)."""
    k = int(np.searchsorted(ms, t_known - M5_MS, side='right')) - 1
    if k < 0 or t_known - M5_MS - ms[k] > max_age_ms:
        return -1
    return k


def features(f):
    tkz, oiz, wall, imbz = [], [], [], []
    for r in f.itertuples():
        fl = flow_hourly(r.pair)
        x_tk = x_oi = np.nan
        if fl is not None:
            ms, tk, oi = fl
            k = int(np.searchsorted(ms, r.ta)) if len(ms) else 0
            if k < len(ms) and ms[k] == r.ta:              # час прокола; известен к ta + 1 ч ≤ решения
                x_tk = -r.dir * tk[k]                      # знак стороны прокола: вниз — продажи «+»
                x_oi = oi[k]
        tkz.append(x_tk)
        oiz.append(x_oi)
        bk = book(r.pair) if r.t >= BOOK_FROM_MS else None
        x_w = x_i = np.nan
        if bk is not None:
            ms, wb, wa, iz = bk
            k = _row_at(ms, r.ta, 30 * 60_000)              # до начала часа прокола
            if k >= 0:
                x_w = wb[k] if r.dir > 0 else wa[k]         # снятие минимума — стакан на покупку
            k = _row_at(ms, r.t, 30 * 60_000)               # к моменту решения
            if k >= 0:
                x_i = r.dir * iz[k]
        wall.append(x_w)
        imbz.append(x_i)
    f = f.copy()
    f['tkz'], f['oiz'], f['wall'], f['imbz'] = tkz, oiz, wall, imbz
    return f


# ── Оценка ───────────────────────────────────────────────────────────────────
def placebo(frame, mask, col='r'):
    """То же плацебо, что smcr_eval.placebo (случайный отбор того же размера
    внутри период × сторона, p односторонний), но распределение суммы отбора
    без возвращения считается по формуле, а не 20 000 перестановками: сделок
    здесь десятки тысяч, перестановки шли бы часами. Нормальное приближение —
    отбор в каждой проверке ≥ сотен сделок."""
    from scipy.stats import norm
    ok = frame[col].notna().to_numpy()
    part = frame[ok].reset_index(drop=True)
    m = np.asarray(mask)[ok]
    r = part[col].to_numpy(dtype=float)
    k_all = int(m.sum())
    if k_all < 5 or k_all == len(m):
        return np.nan, np.nan, k_all
    effect = r[m].mean() - r.mean()
    mean = var = 0.0
    for g in part.groupby(['period', 'dir']).indices.values():
        x, k, n = r[g], int(m[g].sum()), len(g)
        if k == 0:
            continue
        mean += k * x.mean()
        if n > 1:
            var += k * x.var() * (n - k) / (n - 1)
    null_mean = mean / k_all - r.mean()
    sd = np.sqrt(var) / k_all
    p = float(norm.sf((effect - null_mean) / sd)) if sd > 0 else (0.0 if effect > null_mean else 1.0)
    return effect, p, k_all


HYP = {
    'L1 поглощение: TKz ≥ +1': ('tkz', lambda x: x >= 1.0, False),
    'L2 ОИ упал: OIz ≤ −1': ('oiz', lambda x: x <= -1.0, False),
    'L3 стена: WALL ≥ ln 1.5': ('wall', lambda x: x >= np.log(1.5), True),
    'L4 стакан за сделку: IMBz ≥ +1': ('imbz', lambda x: x >= 1.0, True),
}


def evaluate():
    import smcr_eval as V
    from common import ci
    f = pd.read_pickle(os.path.join(OUT, 'absorb_trades.pkl'))
    f = f[(f['period'] != 'fresh') | (f['t'] > V.END_12M)].reset_index(drop=True)   # без двойного счёта
    f = features(f)

    def describe(g):
        r = g['r'].to_numpy(float)
        lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
        return f'{len(r):6d} сд {r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}] итог {r.sum():+8.1f}R'

    print('БАЗА — все снятия, сделка на каждое (вход по рынку, стоп за экстремумом +0.15%, ≥0.8%, цель 2R, ≤48 ч)')
    for sample in ('pool', 'other'):
        g = f[f['sample'] == sample]
        print(f'  {"10 пар пула" if sample == "pool" else "10 пар вне пула":15s} '
              + ' | '.join(f'{p} {describe(g[g["period"] == p])}' for p in PERIODS))
        for tr in ('with', 'against', 'none'):
            h = g[g['trend'] == tr]
            print(f'    тренд {tr:7s}: отбор {describe(h[V.early_mask(h)])} | приёмка {describe(h[V.late_mask(h)])}')
    print('\nПокрытие признаков (доля сделок с значением):')
    for col in ('tkz', 'oiz', 'wall', 'imbz'):
        print(f'  {col:5s} ' + ', '.join(f'{p} {100 * f[f["period"] == p][col].notna().mean():3.0f}%' for p in PERIODS))

    def sel_part(g, book_only):
        e = g[V.early_mask(g)]
        if book_only:
            e = e[e['t'] >= BOOK_FROM_MS]
        return e, g[V.late_mask(g)]

    for subset_name, subset in (('ВСЕ СНЯТИЯ', lambda d: d), ('ТОЛЬКО ПО ТРЕНДУ (сетап 8Б)', lambda d: d[d['trend'] == 'with'])):
        print(f'\nГИПОТЕЗЫ — {subset_name}: эффект к среднему R, p (плацебо внутри период × сторона)')
        passed = []
        for name, (col, rule, book_only) in HYP.items():
            res = {}
            for sample in ('pool', 'other'):
                g = subset(f[(f['sample'] == sample)]).dropna(subset=[col]).reset_index(drop=True)
                early, late = sel_part(g, book_only)
                for part_name, part in (('отбор', early), ('приёмка', late)):
                    part = part.reset_index(drop=True)
                    m = rule(part[col].to_numpy(float))
                    eff, p, n = placebo(part, m)
                    res[(sample, part_name)] = (eff, p, n, len(part),
                                                part['r'].to_numpy(float)[m].mean() if m.sum() else np.nan)
            e0, l0, o1 = res[('pool', 'отбор')], res[('pool', 'приёмка')], res[('other', 'приёмка')]
            print(f'  {name:32s} пул отбор {e0[2]:5d}/{e0[3]:5d} эффект {e0[0]:+.3f} p {e0[1]:.3f} (R {e0[4]:+.3f}) | '
                  f'приёмка {l0[2]:5d}/{l0[3]:5d} эффект {l0[0]:+.3f} p {l0[1]:.3f} (R {l0[4]:+.3f}) | '
                  f'вне пула отбор {res[("other", "отбор")][0]:+.3f} приёмка {o1[0]:+.3f} p {o1[1]:.3f} (R {o1[4]:+.3f})',
                  flush=True)
            if np.isfinite(e0[1]) and e0[1] < 0.05:
                passed.append((name, l0[0], l0[1], o1[0]))
        if subset_name == 'ВСЕ СНЯТИЯ':
            print(f'  прошли отбор: {[x[0] for x in passed] or "нет"}')
            m = len(passed)
            for j, (name, eff, p, eff_o) in enumerate(sorted(passed, key=lambda x: x[2])):
                ok = eff > 0 and p < 0.05 / (m - j) and eff_o > 0
                print(f'  {name}: приёмка эффект {eff:+.3f}, p {p:.3f} (порог Холма {0.05 / (m - j):.4f}), '
                      f'вне пула {eff_o:+.3f} → {"ПРИНЯТА" if ok else "не принята"}')
                if not ok:
                    break

    print('\nR ПО ПЯТЫМ ДОЛЯМ признака (10 пар пула; границы — по отбору; не для приёмки)')
    for col in ('tkz', 'oiz', 'wall', 'imbz'):
        g = f[f['sample'] == 'pool'].dropna(subset=[col])
        early, late = sel_part(g, col in ('wall', 'imbz'))
        edges = np.quantile(early[col], [0.2, 0.4, 0.6, 0.8])
        for part_name, part in (('отбор', early), ('приёмка', late)):
            q = np.searchsorted(edges, part[col].to_numpy(float))
            cells = []
            for j in range(5):
                r = part['r'].to_numpy(float)[q == j]
                cells.append(f'{r.mean() if len(r) else np.nan:+.3f} ({len(r)})')
            print(f'  {col:5s} {part_name:7s} границы {np.round(edges, 2).tolist()}: ' + ' | '.join(cells))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1:2]
    if cmd == ['gen']:
        generate()
    elif cmd == ['sim']:
        simulate()
    elif cmd == ['eval']:
        evaluate()
