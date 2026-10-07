"""
Кэш 2021 года для стенда SMC (П-21, research/smcr_y21.py): свечи 5m/1h и
фандинг 2020-09 … 2022-01, 10 пар пула SMC.

    python research/smcr_y21_fetch.py binance   # → backtest_cache_y21b (Binance UM, data.binance.vision)
    python research/smcr_y21_fetch.py bybit     # → backtest_cache_y21y (Bybit linear, API v5)

У Bybit в 2021 полный год только BTC, LINK, LTC (SOL с 2021-10, AVAX с 2021-09,
BNB с 2021-07) — потому основной замер на Binance, середина ставки та же 1 б.п.

Формат как у research/backtest_cache_*: <PAIR>_<tf>.pkl — список
[ts_ms, o, h, l, c, v]; funding/<PAIR>.csv — timestamp, funding_rate.
"""
import csv
import io
import json
import os
import pickle
import sys
import time
import urllib.request
import zipfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
POOL = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT',
        'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT']
MONTHS = [f'{y}-{m:02d}' for y, m in
          [(2020, 9), (2020, 10), (2020, 11), (2020, 12)] + [(2021, m) for m in range(1, 13)] + [(2022, 1)]]
START_MS = int(pd.Timestamp('2020-09-01', tz='UTC').value // 10 ** 6)
END_MS = int(pd.Timestamp('2022-02-01', tz='UTC').value // 10 ** 6)


def _get(url, tries=4):
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (k + 1))
        except Exception:                                  # noqa: BLE001
            time.sleep(2 * (k + 1))
    raise RuntimeError(url)


def _csv_rows(blob):
    z = zipfile.ZipFile(io.BytesIO(blob))
    text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and not rows[0][0].lstrip('-').isdigit():
        rows = rows[1:]                                    # шапка есть только в новых файлах
    return rows


def save(out, pair, tf, rows):
    rows = sorted({int(r[0]): [int(r[0])] + [float(x) for x in r[1:6]] for r in rows}.values())
    with open(os.path.join(out, f'{pair}_{tf}.pkl'), 'wb') as fh:
        pickle.dump(rows, fh)
    return len(rows)


def binance():
    out = os.path.join(HERE, 'backtest_cache_y21b')
    os.makedirs(os.path.join(out, 'funding'), exist_ok=True)
    base = 'https://data.binance.vision/data/futures/um/monthly'
    for pair in POOL:
        for tf in ('5m', '1h'):
            rows = []
            for m in MONTHS:
                blob = _get(f'{base}/klines/{pair}/{tf}/{pair}-{tf}-{m}.zip')
                if blob:
                    rows += _csv_rows(blob)
            n = save(out, pair, tf, rows)
            print(pair, tf, n, flush=True)
        fund = []
        for m in MONTHS:
            blob = _get(f'{base}/fundingRate/{pair}/{pair}-fundingRate-{m}.zip')
            if blob:
                for r in _csv_rows(blob):
                    # calc_time, funding_interval_hours, last_funding_rate
                    fund.append((int(r[0]), float(r[2])))
        fund.sort()
        with open(os.path.join(out, 'funding', f'{pair}.csv'), 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['timestamp', 'funding_rate'])
            for ts, v in fund:
                # Отметка выплаты округляется до минуты: у Binance calc_time на миллисекунды позже часа.
                w.writerow([pd.Timestamp(ts // 60000 * 60000, unit='ms', tz='UTC'), v])
        print(pair, 'funding', len(fund), flush=True)


def _bybit(path):
    for k in range(8):
        d = json.loads(_get('https://api.bybit.com' + path))
        if d.get('retCode') == 0 and 'list' in (d.get('result') or {}):
            return d
        time.sleep(3 * (k + 1))                           # предел запросов — пауза и повтор
    raise RuntimeError(f'{path}: {d}')


def bybit():
    out = os.path.join(HERE, 'backtest_cache_y21y')
    os.makedirs(os.path.join(out, 'funding'), exist_ok=True)
    for pair in POOL:
        for tf, iv, step in (('5m', '5', 300_000), ('1h', '60', 3_600_000)):
            rows, end = [], END_MS
            while end > START_MS:
                d = _bybit(f'/v5/market/kline?category=linear&symbol={pair}&interval={iv}'
                           f'&start={START_MS}&end={end}&limit=1000')
                got = d['result']['list']                  # новые первыми
                if not got:
                    break
                rows += [r[:6] for r in got]
                oldest = int(got[-1][0])
                if oldest <= START_MS or len(got) < 1000:
                    break
                end = oldest - step
                time.sleep(0.05)
            n = save(out, pair, tf, rows)
            print(pair, tf, n, flush=True)
        fund, end = [], END_MS
        while end > START_MS:
            d = _bybit(f'/v5/market/funding/history?category=linear&symbol={pair}'
                       f'&startTime={START_MS}&endTime={end}&limit=200')
            got = d['result']['list']
            if not got:
                break
            fund += [(int(r['fundingRateTimestamp']), float(r['fundingRate'])) for r in got]
            oldest = min(int(r['fundingRateTimestamp']) for r in got)
            if len(got) < 200:
                break
            end = oldest - 1
            time.sleep(0.05)
        fund = sorted(set(fund))
        with open(os.path.join(out, 'funding', f'{pair}.csv'), 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['timestamp', 'funding_rate'])
            for ts, v in fund:
                w.writerow([pd.Timestamp(ts, unit='ms', tz='UTC'), v])
        print(pair, 'funding', len(fund), flush=True)


if __name__ == '__main__':
    {'binance': binance, 'bybit': bybit}[sys.argv[1]]()
