"""
Минутные свечи Binance (фьючерсы USDⓈ-M) с потоком тейкеров — Binance Vision
`futures/um/{monthly,daily}/klines/<S>/1m` (docs/Микроструктура_на_истории_2026-10-01.md).

В свече Binance есть объём покупок по рынку (taker buy) и число сделок —
это минутная дельта агрессора без скачивания самих сделок.

Выжимка: o, h, l, c, qv (объём в USDT), tbq (покупки тейкеров в USDT),
n (число сделок), float32. Метка — открытие минуты (UTC); свеча известна к её
закрытию. Прошлые месяцы — месячным файлом, если его ещё нет — по дням.
Результат: research/binance_klines_1m_cache/<ПАРА>.pkl.

    python research/fetch_binance_klines_1m.py                 # 30 пар тетради, 2021-01 … вчера
    python research/fetch_binance_klines_1m.py BTCUSDT ETHUSDT # выбранные пары
"""
import io
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'binance_klines_1m_cache')
BASE = 'https://data.binance.vision/data/futures/um/'
MONTHLY = BASE + 'monthly/klines/{s}/1m/{s}-1m-{m}.zip'
DAILY = BASE + 'daily/klines/{s}/1m/{s}-1m-{d}.zip'
START = os.getenv('KL_START', '2021-01-01')
BINANCE = {'SHIB1000USDT': '1000SHIBUSDT'}
COLS = ['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'count',
        'taker_buy_volume', 'taker_buy_quote_volume', 'ignore']
KEEP = {'open': 'o', 'high': 'h', 'low': 'l', 'close': 'c', 'quote_volume': 'qv',
        'taker_buy_quote_volume': 'tbq', 'count': 'n'}
POOL = ('AAVEUSDT', 'ADAUSDT', 'ARBUSDT', 'AVAXUSDT', 'BNBUSDT', 'BTCUSDT', 'COTIUSDT',
        'DOGEUSDT', 'DOTUSDT', 'ETHUSDT', 'LINKUSDT', 'LTCUSDT', 'NEARUSDT', 'SHIB1000USDT',
        'SOLUSDT', 'SUIUSDT', 'UNIUSDT', 'XLMUSDT', 'XRPUSDT', 'ZECUSDT')
EXTRA = ('BCHUSDT', 'ETCUSDT', 'ATOMUSDT', 'FILUSDT', 'TRXUSDT', 'OPUSDT', 'APTUSDT', 'INJUSDT',
         '1000PEPEUSDT', 'SEIUSDT')                     # как Live_Bot/llm_notebook.POOL + EXTRA


def parse(raw):
    head = raw[:64].decode('ascii', 'ignore')
    f = pd.read_csv(io.BytesIO(raw), header=0 if head.startswith('open_time') else None)
    f.columns = COLS[:len(f.columns)]
    t = f['open_time'].astype('int64')
    t = np.where(t > 10 ** 14, t // 1000, t)               # микросекунды в новых файлах
    out = f[list(KEEP)].rename(columns=KEEP).astype(np.float32)
    out.index = pd.to_datetime(t, unit='ms', utc=True)
    return out


def get(url):
    """Содержимое архива; пустой кадр — файла нет (404); None — не скачалось."""
    for n in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}),
                                        timeout=120) as r:
                data = r.read()
            z = zipfile.ZipFile(io.BytesIO(data))
            return parse(z.read(z.namelist()[0]))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return pd.DataFrame()
            time.sleep(1 + n)
        except Exception:                                   # noqa: BLE001
            time.sleep(1 + n)
    return None


def fetch_month(sym, month):
    """Месяц целиком: месячный архив, а если его нет — дни, которые есть."""
    got = get(MONTHLY.format(s=sym, m=month.strftime('%Y-%m')))
    if got is None or not got.empty:
        return got
    last = min(month + pd.offsets.MonthEnd(0), pd.Timestamp.now(tz='UTC').normalize() - pd.Timedelta(days=1))
    days = pd.date_range(month, last, freq='D')
    parts = [get(DAILY.format(s=sym, d=d.strftime('%Y-%m-%d'))) for d in days]
    if any(p is None for p in parts):
        return None
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts) if parts else pd.DataFrame()


def fetch_pair(pair):
    sys.stdout.reconfigure(encoding='utf-8')               # дочерний процесс не проходит __main__
    sym = BINANCE.get(pair, pair)
    path = os.path.join(OUT, pair + '.pkl')
    have = pd.read_pickle(path) if os.path.exists(path) else None
    yesterday = pd.Timestamp.now(tz='UTC').normalize() - pd.Timedelta(days=1)
    months = pd.date_range(pd.Timestamp(START, tz='UTC'), yesterday, freq='MS')
    done = set()
    if have is not None:
        last_of = have.index.to_series().groupby(have.index.strftime('%Y-%m')).max()
        for m in months:                                    # месяц готов, если в нём есть его последний день
            key = m.strftime('%Y-%m')
            if key in last_of.index and last_of[key] >= min(m + pd.offsets.MonthEnd(0), yesterday):
                done.add(m)
    todo = [m for m in months if m not in done]
    parts, failed, empty = [], 0, 0
    started = time.time()
    with ThreadPoolExecutor(6) as pool:
        for got in pool.map(lambda m: fetch_month(sym, m), todo):
            if got is None:
                failed += 1
            elif got.empty:
                empty += 1
            else:
                parts.append(got)
    f = have
    if parts:
        f = pd.concat(([have] if have is not None else []) + parts)
        f = f[~f.index.duplicated(keep='last')].sort_index()
        f.to_pickle(path + '.tmp')
        os.replace(path + '.tmp', path)
    n = 0 if f is None else len(f)
    first = '—' if f is None or not n else f.index[0].strftime('%Y-%m-%d')
    print(f'  {pair:13s} месяцев {len(todo):3d}: скачано {len(parts):3d}, нет файла {empty:3d}, '
          f'не скачалось {failed}; строк {n:8d} с {first}; {time.time() - started:5.0f} с', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    os.makedirs(OUT, exist_ok=True)
    from multiprocessing import Pool
    with Pool(int(os.getenv('KL_PROCS', '4'))) as pool:
        list(pool.imap_unordered(fetch_pair, sys.argv[1:] or POOL + EXTRA))
