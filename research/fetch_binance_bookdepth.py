"""
История стакана Binance (фьючерсы USDⓈ-M) — Binance Vision
`futures/um/daily/bookDepth` (docs/Поглощение_на_снятиях_2026-10-01.md):
каждые 30 секунд накопленная глубина на ±1…5% от цены (монеты и USDT), с 2023-01.

Выжимка: последний снимок каждых 5 минут, глубина в USDT на −2%, −1%, +1%, +2%
(колонки b2, b1, a1, a2). Метка — начало пятиминутки; снимок известен к её концу.
Результат: research/binance_bookdepth_cache/<ПАРА>.pkl (индекс — время UTC).

    python research/fetch_binance_bookdepth.py              # 20 пар списка бота, 2023-01 … вчера
    python research/fetch_binance_bookdepth.py BTCUSDT      # одна пара
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
OUT = os.path.join(HERE, 'binance_bookdepth_cache')
URL = 'https://data.binance.vision/data/futures/um/daily/bookDepth/{s}/{s}-bookDepth-{d}.zip'
START = os.getenv('BD_START', '2023-01-01')
BINANCE = {'SHIB1000USDT': '1000SHIBUSDT'}
LEVELS = {-2: 'b2', -1: 'b1', 1: 'a1', 2: 'a2'}


def reduce_day(raw):
    f = pd.read_csv(io.BytesIO(raw), usecols=['timestamp', 'percentage', 'notional'])
    f = f[f['percentage'].isin(list(LEVELS))]
    f['timestamp'] = pd.to_datetime(f['timestamp'], format='%Y-%m-%d %H:%M:%S', utc=True)
    wide = f.pivot_table(index='timestamp', columns='percentage', values='notional', aggfunc='last')
    wide = wide.rename(columns=LEVELS)
    wide = wide.groupby(wide.index.floor('5min')).last()
    return wide.reindex(columns=list(LEVELS.values())).astype(np.float32)


def fetch_day(sym, day):
    url = URL.format(s=sym, d=day)
    for n in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}),
                                        timeout=60) as r:
                data = r.read()
            z = zipfile.ZipFile(io.BytesIO(data))
            return reduce_day(z.read(z.namelist()[0]))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return pd.DataFrame()                       # пары ещё не было или день не выложен
            time.sleep(1 + n)
        except Exception:                                   # noqa: BLE001
            time.sleep(1 + n)
    return None                                             # не скачалось — отметим


def fetch_pair(pair):
    sys.stdout.reconfigure(encoding='utf-8')               # дочерний процесс не проходит __main__
    sym = BINANCE.get(pair, pair)
    path = os.path.join(OUT, pair + '.pkl')
    have = pd.read_pickle(path) if os.path.exists(path) else None
    days = pd.date_range(pd.Timestamp(START, tz='UTC'),
                         pd.Timestamp.now(tz='UTC').normalize() - pd.Timedelta(days=1), freq='D')
    done = set(have.index.normalize().unique()) if have is not None else set()
    todo = [d for d in days if d not in done]
    parts, failed, empty = [], 0, 0
    started = time.time()
    with ThreadPoolExecutor(8) as pool:
        for got in pool.map(lambda d: fetch_day(sym, d.strftime('%Y-%m-%d')), todo):
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
    print(f'  {pair:13s} дней {len(todo):5d}: скачано {len(parts):5d}, нет файла {empty:4d}, '
          f'не скачалось {failed}; строк {n:7d}; {time.time() - started:5.0f} с', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    os.makedirs(OUT, exist_ok=True)
    sys.path.insert(0, HERE)
    import ai_doctrine as D
    from multiprocessing import Pool
    # Разбор CSV держит GIL: пары — по процессам, дни внутри пары — по потокам.
    with Pool(int(os.getenv('BD_PROCS', '3'))) as pool:
        list(pool.imap_unordered(fetch_pair, sys.argv[1:] or D.PAIRS))
