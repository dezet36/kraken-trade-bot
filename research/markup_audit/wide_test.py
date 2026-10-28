"""
SMC на широком списке пар (PROTOCOL_WIDE.md, записан до расчёта).

    python research/markup_audit/wide_test.py universe   # набор пар каждого периода
    python research/markup_audit/wide_test.py fetch      # свечи и фандинг → backtest_cache_<период>_wide
    python research/markup_audit/wide_test.py gen [--reverse] [--workers=N]   # пары без свечей пропускает
    python research/markup_audit/wide_test.py eval

Набор — на дату начала периода по research/universe_cache: возраст ≥ 90 дней,
не стейблкоин/золото/акция, не пул SMC, 20 верхних по среднему обороту за 30 дней.
"""
import os
import pickle
import shutil
import sys
import time
import warnings
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import structure_test as T                                   # noqa: E402  (пути research и Live_Bot)

RESEARCH = T.RESEARCH
UNIVERSE = os.path.join(RESEARCH, 'universe_cache')
DATES = {'bear': '2022-01-01', 'mid1': '2023-07-01', 'mid2': '2024-07-01', '12m': '2025-05-30',
         'fresh': '2026-07-04'}
SOURCE = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m', 'fresh': 'backtest_cache_fresh20'}
TOP, MIN_AGE_DAYS, TURNOVER_DAYS = 20, 90, 30
# Не криптовалюта: стейблкоины, золото и серебро, акции и индексы (листинги 2025–2026)
NOT_CRYPTO = {'USDC', 'USDE', 'FDUSD', 'DAI', 'TUSD', 'USDD', 'PYUSD', 'BUSD', 'USD1', 'RLUSD', 'EURC',
              'USDT0', 'PAXG', 'XAUT', 'XAU', 'XAG', 'AAPL', 'NVDA', 'TSLA', 'SPY', 'QQQ', 'MSTR', 'COIN',
              'HOOD', 'META', 'GOOGL', 'GOOG', 'AMZN', 'MSFT', 'CRCL', 'NFLX', 'AMD', 'PLTR', 'INTC', 'BABA'}
FRAMES = {'1h': 3_600_000, '5m': 300_000}
SAMPLE = 'wide'


def wide_cache(period):
    return f'backtest_cache_{period}_wide'


def universe(period):
    """20 пар набора периода и таблица отбора."""
    import backtest_smc as bt
    date = pd.Timestamp(DATES[period], tz='UTC')
    listing = pd.read_csv(os.path.join(UNIVERSE, 'listing.csv'))
    listing['first_day'] = pd.to_datetime(listing['first_day'], utc=True)
    rows = []
    for pair, first in zip(listing['pair'], listing['first_day']):
        base = pair[:-4] if pair.endswith('USDT') else pair
        if not pair.endswith('USDT') or base in NOT_CRYPTO or pair in bt.DEFAULT_PAIRS:
            continue
        if (date - first).days < MIN_AGE_DAYS:
            continue
        f = pd.read_csv(os.path.join(UNIVERSE, 'candles', f'{pair}_1d.csv'))
        ts = pd.to_datetime(f['timestamp'], utc=True)
        win = f[(ts < date) & (ts >= date - pd.Timedelta(days=TURNOVER_DAYS))]
        if len(win) < TURNOVER_DAYS * 0.8:
            continue
        rows.append({'pair': pair, 'turnover_musd': win['turnover'].mean() / 1e6, 'listed': first.date()})
    table = pd.DataFrame(rows).sort_values('turnover_musd', ascending=False).reset_index(drop=True)
    return list(table['pair'].head(TOP)), table


# ── Данные ───────────────────────────────────────────────────────────────────
def _bounds(period, tf):
    with open(os.path.join(RESEARCH, SOURCE[period], f'BTCUSDT_{tf}.pkl'), 'rb') as fh:
        raw = pickle.load(fh)
    stamps = sorted(int(r[0]) for r in raw)
    return stamps[0], stamps[-1]


def _download(client, symbol, tf, since, until):
    step = FRAMES[tf]
    out, cursor = {}, since
    while cursor <= until:
        for attempt in range(5):
            try:
                rows = client.fetch_ohlcv(symbol, tf, since=cursor, limit=1000)
                break
            except Exception as exc:                          # noqa: BLE001
                print(f'    {symbol} {tf}: {str(exc)[:80]} — повтор', flush=True)
                time.sleep(2 + attempt * 3)
        else:
            raise RuntimeError(f'{symbol} {tf}: не скачалось')
        if not rows:
            cursor += 1000 * step
            continue
        for r in rows:
            if since <= r[0] <= until:
                out[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:6]]
        nxt = int(rows[-1][0]) + step
        if nxt <= cursor:
            break
        cursor = nxt
    return [out[k] for k in sorted(out)]


def _write(rows, target):
    with open(target + '.tmp', 'wb') as fh:
        pickle.dump(rows, fh)
    os.replace(target + '.tmp', target)


def fetch():
    import ccxt
    client = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
    for period in T.PERIODS:
        src, dst = os.path.join(RESEARCH, SOURCE[period]), os.path.join(RESEARCH, wide_cache(period))
        os.makedirs(os.path.join(dst, 'funding'), exist_ok=True)
        pairs, _ = universe(period)
        lo1, hi1 = _bounds(period, '1h')
        for pair in ['BTCUSDT'] + pairs:                     # BTC — для признаков рынка
            for tf in (('1h',) if pair == 'BTCUSDT' else ('1h', '5m', '4h')):
                target = os.path.join(dst, f'{pair}_{tf}.pkl')
                if os.path.exists(target):
                    continue
                if os.path.exists(os.path.join(src, f'{pair}_{tf}.pkl')):
                    shutil.copyfile(os.path.join(src, f'{pair}_{tf}.pkl'), target)
                    print(f'  {period} {pair} {tf}: копия', flush=True)
                elif tf in FRAMES:
                    lo, hi = _bounds(period, tf)
                    rows = _download(client, pair.replace('USDT', '/USDT:USDT'), tf, lo, hi)
                    _write(rows, target)
                    print(f'  {period} {pair} {tf}: скачано {len(rows)}', flush=True)
            if pair == 'BTCUSDT':
                continue
            target = os.path.join(dst, 'funding', f'{pair}.csv')
            if os.path.exists(target):
                continue
            own = os.path.join(src, 'funding', f'{pair}.csv')
            f = pd.read_csv(own if os.path.exists(own) else os.path.join(UNIVERSE, 'funding', f'{pair}.csv'))
            ts = pd.to_datetime(f['timestamp'], utc=True)
            keep = (ts >= pd.Timestamp(lo1, unit='ms', tz='UTC') - pd.Timedelta(days=40)) & \
                   (ts <= pd.Timestamp(hi1, unit='ms', tz='UTC') + pd.Timedelta(days=1))
            f[keep].to_csv(target + '.tmp', index=False)
            os.replace(target + '.tmp', target)
            print(f'  {period} {pair} фандинг: {int(keep.sum())} ({"кэш периода" if os.path.exists(own) else "universe"})',
                  flush=True)


# ── Стенд ────────────────────────────────────────────────────────────────────
def gen(reverse=False):
    """Строки стенда по паре (structure_test._job); готовые пары oos/x12 — копией."""
    import smc_lab_eval as E
    os.makedirs(os.path.join(T.OUT, 'parts'), exist_ok=True)
    jobs, waiting = [], 0
    for period in T.PERIODS:
        since = E.END_12M_MS - 2 * 86_400_000 if period == 'fresh' else 0
        for pair in universe(period)[0]:
            target = T.part('fractal', SAMPLE, period, pair)
            if os.path.exists(target):
                continue
            for other in ('oos', 'x12'):
                done = T.part('fractal', other, period, pair)
                if os.path.exists(done):
                    shutil.copyfile(done, target)
                    print(f'  {period} {pair}: строки {other}', flush=True)
                    break
            else:
                need = [os.path.join(RESEARCH, wide_cache(period), f'{pair}_{tf}.pkl') for tf in ('1h', '5m')]
                if not all(os.path.exists(x) for x in need):
                    waiting += 1                          # свечи ещё качаются — возьмёт следующий запуск
                    continue
                jobs.append(('fractal', SAMPLE, period, wide_cache(period), pair, since))
    if reverse:
        jobs.reverse()
    print(f'заданий {len(jobs)}, ждут свечей {waiting}', flush=True)
    t0 = time.time()
    with Pool(T.WORKERS) as pool:
        for period, pair, rows, sec in pool.imap_unordered(T._job, jobs):
            if rows is None:
                continue
            T._save(pd.DataFrame(rows), T.part('fractal', SAMPLE, period, pair))
            print(f'  wide {period:6s} {pair:16s} строк {len(rows):6d} {sec:6.0f} с (всего {time.time() - t0:.0f} с)',
                  flush=True)
    for period in T.PERIODS:
        files = [T.part('fractal', SAMPLE, period, p) for p in universe(period)[0]]
        if all(os.path.exists(x) for x in files) and not os.path.exists(T.path('fractal', SAMPLE, period)):
            frames = [x for x in (pd.read_pickle(x) for x in files) if len(x)]
            T._save(pd.concat(frames, ignore_index=True), T.path('fractal', SAMPLE, period))


# ── Портфель ─────────────────────────────────────────────────────────────────
def evaluate():
    import smcr_live as SL
    E = SL.E
    warnings.simplefilter('ignore', RuntimeWarning)
    trades, months, counts = [], {}, []
    for period in T.PERIODS:
        SL.D.PERIODS[period] = wide_cache(period)           # свечи, фандинг и BTC — из папки набора
        pairs = tuple(universe(period)[0])
        f = pd.read_pickle(T.path('fractal', SAMPLE, period)).sort_values(['t', 'pair']).reset_index(drop=True)
        layout = 'wide'
        f['key'] = list(zip(f['pair'], f['poi_type'], f['poi_index'], f['dir'], [layout] * len(f)))
        E._rows[(period, layout)] = f
        E.BAR_H[layout] = 1.0
        base = dict(E.LIVE, pairs=pairs, layouts=(layout,))
        keep = (lambda o: not E.overlaps_12m(o)) if period == 'fresh' else (lambda o: True)
        orders = [o for o in E.orders_for(period, dict(base, filt=lambda r, x: T.against(x))) if keep(o)]
        no_fund = sum(E.funding_data(period, pairs).get(p) is None for p in pairs)
        counts.append(f'{period}: заявок с толпой {len(orders)}, пар без фандинга {no_fund}')
        for variant, (use_crowd, ft) in T.VARIANTS.items():
            spec = dict(base, filt=(lambda r, x: T.against(x)) if use_crowd else None, fill_through=ft)
            out, _ = E.portfolio(period, spec)
            trades += T._trade_rows('wide', variant, period, out, E)
        data = E._exec.get(period, {})
        lo = min(pd.Timestamp(df['timestamp'].iloc[0]).value for df in data.values()) // 10 ** 6
        hi = max(pd.Timestamp(df['timestamp'].iloc[-1]).value for df in data.values()) // 10 ** 6
        months[period] = (hi - (E.END_12M_MS if period == 'fresh' else lo)) / 86_400_000 / 30.44
        E._exec.pop(period, None)
    t = pd.DataFrame(trades)
    t.to_pickle(os.path.join(T.OUT, 'wide_trades.pkl'))
    text = '\n'.join(report(t, months, counts))
    with open(os.path.join(T.OUT, 'wide_eval.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print(text)


def report(t, months, counts):
    pd.set_option('display.width', 250)

    def st(variant, ps):
        return T.stats(t[(t.variant == variant) & t.period.isin(ps)], sum(months[p] for p in ps))

    a, af = st('толпа', T.PERIODS), st('насквозь', T.PERIODS)
    tstat = a['R на сделку'] / (a['±95%'] / 1.96) if a.get('±95%') else np.nan
    plus = sum(st('толпа', [p]).get('итог R', 0) > 0 for p in T.PERIODS)
    checks = [('1. R на сделку > 0 и t ≥ 2', a['R на сделку'] > 0 and tstat >= 2,
               f'{a["сделок"]} сд, {a["R на сделку"]:+.3f}R, t {tstat:+.2f}'),
              ('2. налив насквозь: R на сделку > 0', af['R на сделку'] > 0, f'{af["R на сделку"]:+.3f}R'),
              ('3. итог R > 0 в 3 периодах из 5', plus >= 3, f'{plus} из 5')]
    L = ['=== ПРИЁМКА (PROTOCOL_WIDE.md) ===']
    L += [f'  {n}: {"да" if ok else "НЕТ"} — {x}' for n, ok, x in checks]
    L.append(f'  ИТОГ: {"прошло" if all(ok for _, ok, _ in checks) else "не прошло"}')
    L.append('\nнабор по периодам: ' + '; '.join(f'{p} — {", ".join(universe(p)[0])}' for p in T.PERIODS))
    L.append('месяцев: ' + ', '.join(f'{p} {m:.1f}' for p, m in months.items()))
    L += counts
    for variant in T.VARIANTS:
        L.append(f'\n{variant}, пять периодов:')
        L.append(pd.Series(st(variant, T.PERIODS)).round(3).to_string())
    L.append('\nпо периодам (толпа): сделок | в месяц | плюсовых | R на сделку | итог R | просадка R')
    for p in T.PERIODS:
        s = st('толпа', [p])
        L.append(f'  {p:6s} {s["сделок"]:4d} {s["в месяц"]:5.1f} {s.get("плюсовых", 0):5.0%} '
                 f'{s.get("R на сделку", 0):+.3f} {s.get("итог R", 0):+7.1f} {s.get("макс. просадка R", 0):5.1f}')
    g = t[t.variant == 'толпа']
    L.append('\nпо сторонам (толпа):')
    L.append(g.groupby('side')['R'].agg(['count', 'mean', 'sum']).round(3).to_string())
    L.append('\nпо парам (толпа, все периоды): сделок, R на сделку, итог R')
    L.append(g.groupby('pair')['R'].agg(['count', 'mean', 'sum']).sort_values('sum').round(2).to_string())
    return L


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1]
    if cmd == 'universe':
        pd.set_option('display.width', 200)
        for p in T.PERIODS:
            pairs, table = universe(p)
            print(f'\n{p} ({DATES[p]}): кандидатов {len(table)}')
            print(table.head(TOP + 5).to_string())
    elif cmd == 'fetch':
        fetch()
    elif cmd == 'gen':
        for a in sys.argv[2:]:
            if a.startswith('--workers='):
                T.WORKERS = int(a.split('=')[1])
        gen(reverse='--reverse' in sys.argv)
    elif cmd == 'eval':
        evaluate()
