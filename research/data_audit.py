"""
Аудит данных и разметки рынка (01.10.2026, docs/Аудит_данных_и_разметки_2026-10-01.md).

    python research/data_audit.py candles     # A: свечи Bybit — пропуски, повторы, нулевой объём
    python research/data_audit.py venues      # B: свечи Binance (flow_cache) против Bybit
    python research/data_audit.py funding     # C: фандинг — две копии, дыры
    python research/data_audit.py structure   # D: разметка — уровни окна, свинги, тренд, блоки, имбалансы, ликвидность
Каждая часть печатает свою таблицу; сводка — в документе.
"""
import os
import pickle
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

from infra import logger  # noqa: E402
logger.log = lambda *a, **k: None

CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m', 'fresh': 'backtest_cache_fresh20'}
POOL10 = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT']
H = 3_600_000


def raw(cache, pair, tf):
    p = os.path.join(HERE, cache, f'{pair}_{tf}.pkl')
    if not os.path.exists(p):
        return None
    with open(p, 'rb') as fh:
        return np.array(pickle.load(fh), dtype=float)


# ── A. Свечи Bybit ───────────────────────────────────────────────────────────
def candles():
    import ai_doctrine as D
    rows = []
    for period, cache in CACHES.items():
        for pair in D.PAIRS:
            for tf, step in (('1h', H), ('5m', 300_000)):
                a = raw(cache, pair, tf)
                if a is None or not len(a):
                    continue
                ts = a[:, 0].astype('int64')
                dup = len(ts) - len(np.unique(ts))
                u = np.unique(ts)
                diffs = np.diff(u)
                expected = (u[-1] - u[0]) // step + 1
                missing = int(expected - len(u))
                max_gap = int(diffs.max() // step) - 1 if len(diffs) else 0
                vol0 = int((a[:, 5] <= 0).sum())
                flat = int((a[:, 2] == a[:, 3]).sum())
                rows.append({'period': period, 'pair': pair, 'tf': tf, 'bars': len(u), 'missing': missing,
                             'missing_pct': 100 * missing / expected, 'max_gap_bars': max_gap, 'dups': dup,
                             'vol0_pct': 100 * vol0 / len(a), 'flat_pct': 100 * flat / len(a)})
    f = pd.DataFrame(rows)
    pd.set_option('display.width', 220)
    print('A. Свечи Bybit в кэшах периодов (20 пар списка бота)')
    print(f.groupby(['tf', 'period']).agg(pairs=('pair', 'nunique'), bars=('bars', 'sum'), missing=('missing', 'sum'),
                                          max_gap=('max_gap_bars', 'max'), dups=('dups', 'sum'),
                                          vol0_pct=('vol0_pct', 'mean'), flat_pct=('flat_pct', 'mean')).round(3).to_string())
    worst = f.sort_values('missing_pct', ascending=False).head(8)
    print('\nхудшие по пропускам:')
    print(worst[['period', 'pair', 'tf', 'missing', 'missing_pct', 'max_gap_bars']].round(3).to_string(index=False))
    print('\nнулевой объём / плоские свечи, худшие пары (1h):')
    w = f[f.tf == '1h'].groupby('pair')[['vol0_pct', 'flat_pct']].mean().sort_values('flat_pct', ascending=False).head(6)
    print(w.round(2).to_string())


# ── B. Binance против Bybit ──────────────────────────────────────────────────
def venues():
    rows, newhigh = [], []
    for period, cache in CACHES.items():
        for pair in POOL10:
            a = raw(cache, pair, '1h')
            fp = os.path.join(HERE, 'flow_cache', pair + '.pkl')
            if a is None or not os.path.exists(fp):
                continue
            by = pd.DataFrame(a[:, 1:6], index=pd.to_datetime(a[:, 0].astype('int64'), unit='ms', utc=True),
                              columns=['o', 'h', 'l', 'c', 'v'])
            by = by[~by.index.duplicated()]
            bn = pd.read_pickle(fp)[['o', 'h', 'l', 'c', 'v']]
            j = by.join(bn, how='inner', lsuffix='_by', rsuffix='_bn').dropna()
            if len(j) < 100:
                continue
            ref = j['c_by']
            d = {k: (j[f'{k}_bn'] / j[f'{k}_by'] - 1) * 1e4 for k in ('c', 'h', 'l')}
            # «новый максимум суток»: одна биржа обновила максимум прошлых 24 ч, другая — нет
            nh_by = j['h_by'] > j['h_by'].shift(1).rolling(24).max()
            nh_bn = j['h_bn'] > j['h_bn'].shift(1).rolling(24).max()
            nl_by = j['l_by'] < j['l_by'].shift(1).rolling(24).min()
            nl_bn = j['l_bn'] < j['l_bn'].shift(1).rolling(24).min()
            ev = nh_by | nh_bn
            dis_h = (nh_by != nh_bn)[ev].mean()
            ev_l = nl_by | nl_bn
            dis_l = (nl_by != nl_bn)[ev_l].mean()
            rows.append({'period': period, 'pair': pair, 'hours': len(j),
                         'close_med_bp': d['c'].abs().median(), 'close_p99_bp': d['c'].abs().quantile(0.99),
                         'high_med_bp': d['h'].abs().median(), 'high_p99_bp': d['h'].abs().quantile(0.99),
                         'low_med_bp': d['l'].abs().median(), 'low_p99_bp': d['l'].abs().quantile(0.99),
                         'wick_gt10bp_pct': 100 * ((d['h'].abs() > 10) | (d['l'].abs() > 10)).mean(),
                         'newhigh_disagree_pct': 100 * dis_h, 'newlow_disagree_pct': 100 * dis_l,
                         'vol_ratio_bn_by': (j['v_bn'] / j['v_by']).median()})
    f = pd.DataFrame(rows)
    pd.set_option('display.width', 220)
    print('B. Часовые свечи Binance (flow_cache) против Bybit (кэши периодов), 10 пар пула; расхождение, б.п.')
    print(f.groupby('period')[['hours', 'close_med_bp', 'close_p99_bp', 'high_med_bp', 'high_p99_bp', 'low_med_bp',
                               'low_p99_bp', 'wick_gt10bp_pct', 'newhigh_disagree_pct', 'newlow_disagree_pct',
                               'vol_ratio_bn_by']].agg({'hours': 'sum', **{c: 'mean' for c in f.columns[3:]}}).round(2).to_string())
    print('\nпо парам (все периоды):')
    print(f.groupby('pair')[['close_med_bp', 'high_p99_bp', 'wick_gt10bp_pct', 'newhigh_disagree_pct',
                             'newlow_disagree_pct']].mean().round(2).to_string())


# ── C. Фандинг: две копии ────────────────────────────────────────────────────
def funding():
    import ai_setups as S
    rows = []
    for period, cache in CACHES.items():
        for pair in POOL10:
            got = S.load_series(cache, 'funding', pair, 'funding_rate')
            fp = os.path.join(HERE, 'flow_cache', pair + '.pkl')
            if got is None or not os.path.exists(fp):
                continue
            ts, vals = got
            fl = pd.read_pickle(fp)['funding']
            idx = pd.to_datetime(ts, unit='ms', utc=True)
            s_cache = pd.Series(vals, index=idx)
            s_cache = s_cache[~s_cache.index.duplicated()]
            j = pd.concat([s_cache.rename('cache'), fl.rename('flow')], axis=1, join='inner').dropna()
            if not len(j):
                continue
            rows.append({'period': period, 'pair': pair, 'payments': len(j),
                         'equal_pct': 100 * np.isclose(j['cache'], j['flow'], atol=1e-9).mean(),
                         'max_diff_bp': (j['cache'] - j['flow']).abs().max() * 1e4,
                         'at_mid_pct': 100 * np.isclose(j['cache'], 0.0001, atol=1e-9).mean()})
    f = pd.DataFrame(rows)
    print('C. Фандинг: кэш периода (Bybit, fetch_positioning) против flow_cache (Bybit, ai_flow_fetch)')
    print(f.groupby('period')[['payments', 'equal_pct', 'max_diff_bp', 'at_mid_pct']].agg(
        {'payments': 'sum', 'equal_pct': 'mean', 'max_diff_bp': 'max', 'at_mid_pct': 'mean'}).round(2).to_string())
    holes = []
    for folder in ('flow_cache_2021', 'flow_cache', 'flow_cache_new', 'flow_cache_extra'):
        for n in sorted(os.listdir(os.path.join(HERE, folder))):
            if n.endswith('.pkl'):
                x = pd.read_pickle(os.path.join(HERE, folder, n))
                for col in ('funding', 'oi', 'buy_ratio'):
                    if col in x:
                        nan = x[col].isna().mean()
                        if nan > 0.01:
                            holes.append((folder, n[:-4], col, round(100 * nan, 1)))
    print('\nдыры (>1% часов без значения) в flow_cache*:')
    for h in holes:
        print('  ', h)


# ── D. Разметка ──────────────────────────────────────────────────────────────
def _load_pair(period, pair):
    import backtest_smc as bt
    bt.CACHE_DIR = os.path.join(HERE, CACHES[period])
    df1 = bt.load_cached(pair, '1h')
    if df1 is None:
        return None
    df4 = bt.load_cached(pair, '4h')
    if df4 is None:
        df4 = bt.resample(df1, '4h')
    return {'1h': df1, '4h': df4, '1d': bt.resample(df1, '1D')}


def ref_levels_window():
    """Уровни прошлого дня/недели/месяца на окне живого бота (304 закрытых часа)
    против той же метки по полной истории."""
    from strategies.smc import liquidity
    out = []
    for pair in POOL10:
        data = _load_pair('mid2', pair)
        df = data['1h']
        full = liquidity.build_reference_levels(df)
        for end in range(1000, len(df), 24):
            win = df.iloc[end - 304:end].reset_index(drop=True)
            lv = liquidity.build_reference_levels(win)
            last_w = lv.iloc[-1]
            last_f = full.iloc[end - 1]
            row = {'pair': pair}
            for col in ('pdh', 'pdl', 'pwh', 'pwl', 'pmh', 'pml'):
                a, b = last_w[col], last_f[col]
                row[col] = (np.isfinite(a) and np.isfinite(b) and abs(a - b) / b < 1e-9)
                row[col + '_nan'] = not np.isfinite(a)
            out.append(row)
    f = pd.DataFrame(out)
    print('D1. Уровни прошлого периода на окне живого бота (304 часа) против полной истории, 10 пар, mid2,')
    print('    раз в сутки; доля совпадений / доля «нет значения» в окне:')
    for col in ('pdh', 'pdl', 'pwh', 'pwl', 'pmh', 'pml'):
        print(f'    {col.upper():4s} совпадает {100 * f[col].mean():5.1f}%   нет значения {100 * f[col + "_nan"].mean():5.1f}%')


def swings_ties():
    from strategies.smc import swings
    rows = []
    for pair in POOL10:
        data = _load_pair('mid2', pair)
        for tf in ('1h', '4h'):
            df = data[tf]
            hs, ls = swings.find_swings(df, n=2, soft_right=False)
            hs2, ls2 = swings.find_swings(df, n=2, soft_right=True)
            # «пропавшие» экстремумы: окно 5 свечей, максимум достигнут, но не единственный
            h = df['high'].to_numpy(float)
            tie = 0
            for i in range(2, len(h) - 2):
                w = h[i - 2:i + 3]
                if h[i] == w.max() and (w == h[i]).sum() > 1 and h[i - 1] != h[i] and h[i - 2] != h[i]:
                    tie += 1
            rows.append({'pair': pair, 'tf': tf, 'swing_highs': len(hs), 'with_soft_right': len(hs2),
                         'lost_pct': 100 * (len(hs2) - len(hs)) / max(1, len(hs2)), 'ties_left_first': tie})
    f = pd.DataFrame(rows)
    print('D2. Свинги и равные вершины (mid2): строгое сравнение справа теряет вершину, если её повторили')
    print(f.groupby('tf')[['swing_highs', 'with_soft_right', 'lost_pct']].agg(
        {'swing_highs': 'sum', 'with_soft_right': 'sum', 'lost_pct': 'mean'}).round(2).to_string())
    print(f[f.tf == '1h'].sort_values('lost_pct', ascending=False).head(5)[['pair', 'lost_pct']].round(2).to_string(index=False))


def trend_flips():
    from strategies.smc import structure as S
    rows = []
    for pair in POOL10:
        data = _load_pair('mid2', pair)
        for tf in ('1h', '4h', '1d'):
            st = S.build_structure(data[tf], tier='swing')
            ev = st['events']
            dirs = [e['direction'] for e in ev]
            flips = [i for i in range(1, len(dirs)) if dirs[i] != dirs[i - 1]]
            idx = [ev[i]['index'] for i in flips]
            gaps = np.diff(idx) if len(idx) > 1 else np.array([np.nan])
            n = len(data[tf])
            rows.append({'pair': pair, 'tf': tf, 'bars': n, 'events_per_100': 100 * len(ev) / n,
                         'flips_per_100': 100 * len(flips) / n, 'median_bars_between_flips': float(np.nanmedian(gaps)),
                         'choch_share': np.mean([e['type'].endswith('CHOCH') for e in ev]) if ev else np.nan})
    f = pd.DataFrame(rows)
    print('D3. Сломы структуры (фрактал 5 свечей, закрытие за свингом), mid2, 10 пар:')
    print(f.groupby('tf')[['events_per_100', 'flips_per_100', 'median_bars_between_flips', 'choch_share']].mean()
          .round(2).to_string())


def ob_position():
    from strategies.smc import poi, structure as S
    rows = []
    for pair in POOL10:
        df = _load_pair('mid2', pair)['1h']
        st = S.build_structure(df, tier='swing')
        hi, lo = df['high'].to_numpy(float), df['low'].to_numpy(float)
        for b in poi.find_order_blocks(df, st):
            brk = b['break_index']
            ev = next((e for e in st['events'] if e['index'] == brk), None)
            if ev is None:
                continue
            a = max(0, min(ev.get('broken_index', brk - 20), brk - 1), brk - 20)
            if b['direction'] == 'BULLISH':
                o = a + int(np.argmin(lo[a:brk + 1]))
                span = hi[brk] - lo[o]
                pos = (b['top'] - lo[o]) / span if span > 0 else np.nan
            else:
                o = a + int(np.argmax(hi[a:brk + 1]))
                span = hi[o] - lo[brk]
                pos = (hi[o] - b['bottom']) / span if span > 0 else np.nan
            rows.append({'pos': pos, 'bars_from_origin': b['index'] - o, 'at_origin': b['index'] in (o, o - 1)})
    f = pd.DataFrame(rows)
    print('D4. Где стоит ордер-блок ядра на импульсе, 1 ч, mid2, 10 пар '
          f'({len(f)} блоков): 0 — начало импульса, 1 — точка слома')
    print(f'    ближний край блока: медиана {f["pos"].median():.2f}; в верхней половине импульса '
          f'{100 * (f["pos"] > 0.5).mean():.0f}%; блок — свеча начала импульса или перед ней {100 * f["at_origin"].mean():.0f}%; '
          f'свечей от начала импульса до блока — медиана {f["bars_from_origin"].median():.0f}')


def _atr(df, n=14):
    h, l, c = (df[k].to_numpy(float) for k in ('high', 'low', 'close'))
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(abs(h - prev), abs(l - prev)))
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


def fvg_magnet(horizon=24):
    """Возвращается ли цена к имбалансу чаще, чем к зоне того же размера на том же
    расстоянии в случайный час той же пары."""
    from strategies.smc import imbalance
    rng = np.random.default_rng(1)
    rows = []
    for period in ('bear', 'mid2', 'fresh'):
        for pair in POOL10:
            data = _load_pair(period, pair)
            if data is None:
                continue
            df = data['1h']
            h, l, c = (df[k].to_numpy(float) for k in ('high', 'low', 'close'))
            atr = _atr(df)
            n = len(df)
            for g in imbalance.find_fvg(df):
                i = g['index']
                if i + horizon >= n or not np.isfinite(atr[i]) or atr[i] <= 0:
                    continue
                size = (g['top'] - g['bottom']) / atr[i]
                if g['direction'] == 'BULLISH':
                    d = (c[i] - g['top']) / atr[i]
                    half = g['top'] - 0.5 * (g['top'] - g['bottom'])
                    fill = l[i + 1:i + 1 + horizon].min() <= half
                else:
                    d = (g['bottom'] - c[i]) / atr[i]
                    half = g['bottom'] + 0.5 * (g['top'] - g['bottom'])
                    fill = h[i + 1:i + 1 + horizon].max() >= half
                if d < 0:
                    continue
                # плацебо: случайный час, та же сторона, то же расстояние и размер в ATR
                j = int(rng.integers(30, n - horizon - 1))
                if not np.isfinite(atr[j]):
                    continue
                if g['direction'] == 'BULLISH':
                    lvl = c[j] - (d + 0.5 * size) * atr[j]
                    pfill = l[j + 1:j + 1 + horizon].min() <= lvl
                else:
                    lvl = c[j] + (d + 0.5 * size) * atr[j]
                    pfill = h[j + 1:j + 1 + horizon].max() >= lvl
                rows.append({'period': period, 'pair': pair, 'dir': g['direction'], 'size_atr': size,
                             'size_pct': g['size_pct'] * 100, 'fill': fill, 'placebo': pfill})
    f = pd.DataFrame(rows)
    print(f'D5. Имбалансы 1 ч (≥0.10% цены): заполнение наполовину за {horizon} ч против случайной зоны того же '
          f'размера на том же расстоянии ({len(f)} имбалансов)')
    print(f.groupby('period')[['fill', 'placebo']].mean().mul(100).round(1).to_string())
    print(f'    всего: имбаланс {100 * f["fill"].mean():.1f}%, случайная зона {100 * f["placebo"].mean():.1f}%')
    print('    размер имбаланса в ATR по парам (медиана): '
          + ', '.join(f'{p[:-4]} {v:.2f}' for p, v in f.groupby('pair')['size_atr'].median().items()))
    print(f'    порог 0.10% цены в ATR часа: от {f.groupby("pair")["size_atr"].min().min():.2f} до '
          f'{f.groupby("pair")["size_atr"].min().max():.2f} — порог фиксирован в процентах, а не в волатильности')


def liquidity_magnet(horizon=24):
    """Доходит ли цена до неснятого свинг-хая/лоя (пула) чаще, чем до уровня на том же
    расстоянии в ATR в случайный час той же пары."""
    from strategies.smc import swings
    rng = np.random.default_rng(2)
    rows = []
    for period in ('bear', 'mid2', 'fresh'):
        for pair in POOL10:
            data = _load_pair(period, pair)
            if data is None:
                continue
            df = data['1h']
            h, l, c = (df[k].to_numpy(float) for k in ('high', 'low', 'close'))
            atr = _atr(df)
            n = len(df)
            hs, ls = swings.find_swings(df, n=2)
            arrs = {}
            for kind, pts in (('BSL', hs), ('SSL', ls)):
                price = np.array([p['price'] for p in pts], float)
                conf = np.array([p['confirmed_at'] for p in pts], int)
                touch = np.full(len(pts), n + 10, int)          # первая свеча, снявшая свинг
                for k, p in enumerate(pts):
                    seg = (h[p['index'] + 1:] >= p['price']) if kind == 'BSL' else (l[p['index'] + 1:] <= p['price'])
                    if seg.any():
                        touch[k] = p['index'] + 1 + int(np.argmax(seg))
                arrs[kind] = (price, conf, touch)
            # для каждой 12-й свечи: ближайший неснятый подтверждённый свинг сверху и снизу
            for t in range(200, n - horizon, 12):
                if not np.isfinite(atr[t]):
                    continue
                for kind in ('BSL', 'SSL'):
                    price, conf, touch = arrs[kind]
                    dist = (price - c[t]) if kind == 'BSL' else (c[t] - price)
                    m = (conf <= t) & (touch > t) & (dist > 0)
                    if not m.any():
                        continue
                    k = int(np.flatnonzero(m)[np.argmin(dist[m])])
                    best = (dist[k], price[k])
                    d_atr = best[0] / atr[t]
                    if d_atr > 6:
                        continue
                    hit = (h[t + 1:t + 1 + horizon].max() >= best[1]) if kind == 'BSL' else \
                        (l[t + 1:t + 1 + horizon].min() <= best[1])
                    j = int(rng.integers(200, n - horizon - 1))
                    if not np.isfinite(atr[j]):
                        continue
                    lvl = c[j] + d_atr * atr[j] if kind == 'BSL' else c[j] - d_atr * atr[j]
                    phit = (h[j + 1:j + 1 + horizon].max() >= lvl) if kind == 'BSL' else (l[j + 1:j + 1 + horizon].min() <= lvl)
                    rows.append({'period': period, 'kind': kind, 'd_atr': d_atr, 'hit': hit, 'placebo': phit})
    f = pd.DataFrame(rows)
    f['bin'] = pd.cut(f['d_atr'], [0, 1, 2, 3, 6])
    print(f'D6. Ликвидность (ближайший неснятый свинг 1 ч): дошла ли цена за {horizon} ч — против уровня на том же '
          f'расстоянии в случайный час ({len(f)} замеров)')
    print(f.groupby(['bin'], observed=True)[['hit', 'placebo']].mean().mul(100).round(1).to_string())
    print(f.groupby(['period'])[['hit', 'placebo']].mean().mul(100).round(1).to_string())


def structure():
    ref_levels_window()
    print()
    swings_ties()
    print()
    trend_flips()
    print()
    ob_position()
    print()
    fvg_magnet()
    print()
    liquidity_magnet()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    part = sys.argv[1] if len(sys.argv) > 1 else 'candles'
    {'candles': candles, 'venues': venues, 'funding': funding, 'structure': structure}[part]()
