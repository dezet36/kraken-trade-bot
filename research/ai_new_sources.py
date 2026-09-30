"""
Новые виды данных для поиска закономерностей (30.09.2026, docs/ИИ_замечания_на_проверку.md, п. 82).

Спот Binance (оборот и покупки по рынку), Coinbase (цена в USD — спрос США),
Deribit DVOL (ожидаемая волатильность BTC). История — с 2020-11, свои папки
research/flow_cache_src/{spot,cb,dvol}/ (не в git). Признаки добавляются к
существующим столбцами — прежние значения не меняются.

    python research/ai_new_sources.py fetch            # скачать всё (42 монеты тетради + DVOL)
    python research/ai_new_sources.py coverage         # покрытие по периодам
"""
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'flow_cache_src')
START = pd.Timestamp('2020-11-01', tz='UTC')
H = 3_600_000
SPOT_SYMBOL = {'SHIB1000USDT': 'SHIBUSDT', '1000PEPEUSDT': 'PEPEUSDT'}
CB_BASE = {'SHIB1000USDT': 'SHIB', '1000PEPEUSDT': 'PEPE'}

NEW_FEATURES = {
    'spot_share_24h': ('доля спота в обороте за сутки: спот / (спот + фьючерс)',
                       'share of spot in 24h turnover: spot / (spot + perpetual); higher = spot-driven market'),
    'spot_share_rel': ('доля спота к своей медиане за 30 дней (> 1 — спот активнее обычного)',
                       'spot_share_24h divided by its 30-day median (>1 = spot unusually active, <1 = futures-driven)'),
    'spot_taker_24h': ('доля покупок по рынку на споте за сутки (0.5 — поровну)',
                       'aggressive-buy share on the SPOT market over 24h; 0.5 = balanced'),
    'taker_gap_24h': ('spot_taker_24h − taker_24h фьючерса (плюс — спот агрессивнее покупает)',
                      'spot_taker_24h minus futures taker_24h (positive = spot buyers more aggressive than futures)'),
    'cb_prem_bp': ('премия Coinbase (USD) к споту Binance, б.п., среднее за 4 ч',
                   'Coinbase (US) price premium over Binance spot, basis points, 4h average (positive = US buyers pay up)'),
    'cb_prem_chg_24h': ('изменение премии Coinbase за сутки, б.п.',
                        'change of cb_prem_bp over 24h, basis points'),
    'btc_cb_prem_bp': ('премия Coinbase у BTC, б.п. — спрос США на рынке в целом',
                       'Coinbase premium of BTC, basis points (US demand for the whole market)'),
    'dvol': ('DVOL — ожидаемая волатильность BTC из опционов Deribit, % годовых',
             'DVOL: BTC implied volatility from Deribit options, % annualized (fear gauge)'),
    'dvol_chg_24h': ('изменение DVOL за сутки, %', 'DVOL change over 24h, %'),
    'dvol_pct_30d': ('ранг DVOL за 30 дней, 0..1', 'percentile of DVOL within the last 30 days, 0..1'),
}


def _get(url, tries=6):
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'kraken-research'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as exc:                       # noqa: BLE001
            if n == tries - 1:
                raise
            time.sleep(1.5 * (n + 1) + (5 if '429' in str(exc) else 0))


def _save(df, kind, name):
    folder = os.path.join(SRC, kind)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'{name}.pkl')
    df.to_pickle(path + '.tmp')
    os.replace(path + '.tmp', path)


def fetch_spot(pair):
    sym = SPOT_SYMBOL.get(pair, pair)
    now = int(time.time() * 1000) // H * H
    rows, start = [], int(START.timestamp() * 1000)
    while start < now:
        try:
            batch = _get(f'https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&startTime={start}&limit=1000')
        except Exception:                              # noqa: BLE001
            break                                      # нет такой пары на споте
        if not batch:
            break
        rows += [b for b in batch if b[0] < now]
        start = batch[-1][0] + H
        time.sleep(0.1)
    df = pd.DataFrame([(r[0], float(r[4]), float(r[7]), float(r[10])) for r in rows],
                      columns=['ts', 'spot_c', 'spot_qv', 'spot_tbq']).drop_duplicates('ts')
    df.index = pd.to_datetime(df.pop('ts'), unit='ms', utc=True)
    _save(df, 'spot', pair)
    return f'{pair} спот: {len(df)} ч' + (f' с {df.index[0]:%Y-%m-%d}' if len(df) else '')


def fetch_cb(pair):
    base = CB_BASE.get(pair, pair[:-4])
    end_all = pd.Timestamp.now(tz='UTC').floor('h')
    out, t = {}, START
    while t < end_all:
        e = min(t + pd.Timedelta(hours=299), end_all)
        try:
            batch = _get(f'https://api.exchange.coinbase.com/products/{base}-USD/candles?granularity=3600'
                         f'&start={t:%Y-%m-%dT%H:%M:%SZ}&end={e:%Y-%m-%dT%H:%M:%SZ}')
        except Exception:                              # noqa: BLE001
            batch = None
        if isinstance(batch, dict):                    # {"message": "NotFound"} — пары нет на Coinbase
            break
        for row in batch or []:
            out[int(row[0]) * 1000] = float(row[4])
        t = e + pd.Timedelta(hours=1)
        time.sleep(0.35)
    df = pd.DataFrame({'cb_c': pd.Series(out, dtype=float)}).sort_index()
    df.index = pd.to_datetime(df.index, unit='ms', utc=True)
    _save(df, 'cb', pair)
    return f'{pair} Coinbase: {len(df)} ч' + (f' с {df.index[0]:%Y-%m-%d}' if len(df) else '')


def fetch_dvol(currency='BTC'):
    out, end = {}, int(time.time() * 1000)
    stop = int(START.timestamp() * 1000)
    while end > stop:
        r = _get(f'https://www.deribit.com/api/v2/public/get_volatility_index_data?currency={currency}'
                 f'&start_timestamp={stop}&end_timestamp={end}&resolution=3600')['result']
        data = r.get('data') or []
        if not data:
            break
        for ts, _o, _h, _l, c in data:
            out[int(ts)] = float(c)
        oldest = min(int(x[0]) for x in data)
        if oldest >= end:
            break
        end = oldest - 1
        time.sleep(0.1)
    df = pd.DataFrame({'dvol': pd.Series(out, dtype=float)}).sort_index()
    df.index = pd.to_datetime(df.index, unit='ms', utc=True)
    _save(df, 'dvol', currency)
    return f'DVOL {currency}: {len(df)} ч' + (f' с {df.index[0]:%Y-%m-%d}' if len(df) else '')


def _load(kind, name):
    path = os.path.join(SRC, kind, f'{name}.pkl')
    return pd.read_pickle(path) if os.path.exists(path) else None


def enrich(data):
    """{пара: (таблица, признаки)} -> те же признаки + новые столбцы (на месте). Пары без источника — NaN."""
    if getattr(enrich, '_done', None) is data:
        return data
    dvol = _load('dvol', 'BTC')
    cb_prem = {}
    for pair, (df, f) in data.items():
        idx = f.index
        spot = _load('spot', pair)
        spot = spot.reindex(idx) if spot is not None else pd.DataFrame(index=idx, columns=['spot_c', 'spot_qv', 'spot_tbq'],
                                                                       dtype=float)
        sqv = spot['spot_qv'].rolling(24, min_periods=20).sum()
        pqv = df['qv'].reindex(idx).rolling(24, min_periods=20).sum()
        share = sqv / (sqv + pqv)
        f['spot_share_24h'] = share
        f['spot_share_rel'] = share / share.rolling(720, min_periods=480).median()
        f['spot_taker_24h'] = spot['spot_tbq'].rolling(24, min_periods=20).sum() / sqv
        f['taker_gap_24h'] = f['spot_taker_24h'] - f['taker_24h']
        cb = _load('cb', pair)
        if cb is not None and len(cb):
            prem = ((cb['cb_c'].reindex(idx) / spot['spot_c']) - 1) * 1e4
            prem = prem.rolling(4, min_periods=3).mean()
        else:
            prem = pd.Series(np.nan, index=idx)
        f['cb_prem_bp'] = prem
        f['cb_prem_chg_24h'] = prem - prem.shift(24)
        cb_prem[pair] = prem
    btc = cb_prem.get('BTCUSDT')
    for pair, (df, f) in data.items():
        f['btc_cb_prem_bp'] = btc.reindex(f.index) if btc is not None else np.nan
        if dvol is not None:
            d = dvol['dvol'].reindex(f.index)
            f['dvol'] = d
            f['dvol_chg_24h'] = (d / d.shift(24) - 1) * 100
            f['dvol_pct_30d'] = d.rolling(720, min_periods=480).rank(pct=True)
        else:
            f['dvol'] = f['dvol_chg_24h'] = f['dvol_pct_30d'] = np.nan
    enrich._done = data
    return data


def install():
    """Подключить новые признаки к стенду: L.load_all отдаёт обогащённые данные, признаки — в описаниях."""
    sys.path.insert(0, HERE)
    import ai_pattern_lab as L
    for name, (ru, _en) in NEW_FEATURES.items():
        L.FEATURES.setdefault(name, ru)
    if not getattr(L.load_all, '_enriched', False):
        original = L.load_all

        def load_all(*a, **k):
            return enrich(original(*a, **k))
        load_all._enriched = True
        L.load_all = load_all
    try:
        import ai_model_research as M
        for name, (_ru, en) in NEW_FEATURES.items():
            M.DESCR_EN.setdefault(name, en)
    except ImportError:
        pass
    return L


def coverage():
    import ai_notebook_2021  # noqa: F401  — L.SPLITS['y2021']
    import ai_pattern_lab as L
    periods = {k: L.SPLITS[k] for k in ('y2021', 'train', 'valid')}
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Live_Bot'))
    os.environ.setdefault('BOT_DATA_DIR', os.path.join(HERE, 'results'))
    import llm_notebook as N
    lines = ['Покрытие новых источников (доля часов периода): спот / Coinbase; DVOL — общий']
    for p in N.UNIVERSE:
        cells = []
        for kind, col in (('spot', 'spot_qv'), ('cb', 'cb_c')):
            df = _load(kind, p)
            for name, (a, b) in periods.items():
                a, b = pd.Timestamp(a, tz='UTC'), pd.Timestamp(b, tz='UTC')
                hours = (b - a) / pd.Timedelta(hours=1)
                n = 0 if df is None else int(((df.index >= a) & (df.index < b) & df[col].notna()).sum())
                cells.append(f'{n / hours:4.0%}')
        lines.append(f'  {p:14s} спот ' + ' '.join(cells[:3]) + ' | Coinbase ' + ' '.join(cells[3:]))
    d = _load('dvol', 'BTC')
    for name, (a, b) in periods.items():
        a, b = pd.Timestamp(a, tz='UTC'), pd.Timestamp(b, tz='UTC')
        n = 0 if d is None else int(((d.index >= a) & (d.index < b)).sum())
        lines.append(f'  DVOL {name}: {n / ((b - a) / pd.Timedelta(hours=1)):.0%}')
    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', 'ai_new_sources_coverage.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'fetch'
    if cmd == 'fetch':
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Live_Bot'))
        os.environ.setdefault('BOT_DATA_DIR', os.path.join(HERE, 'results'))
        import llm_notebook as N
        pairs = sys.argv[2:] or list(N.UNIVERSE)
        print(fetch_dvol(), flush=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for msg in pool.map(fetch_spot, pairs):
                print(msg, flush=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for msg in pool.map(fetch_cb, pairs):
                print(msg, flush=True)
    elif cmd == 'coverage':
        coverage()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
