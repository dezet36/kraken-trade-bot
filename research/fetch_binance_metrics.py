"""
История позиционирования Binance (фьючерсы USDⓈ-M), 5 минут — Binance Vision
`futures/um/daily/metrics` (docs/Аудит_данных_и_разметки_2026-10-01.md, раздел 4):

    sum_open_interest, sum_open_interest_value     — ОИ (монеты / USDT)
    count_toptrader_long_short_ratio                — лонг/шорт крупных трейдеров по СЧЕТАМ
    sum_toptrader_long_short_ratio                  — лонг/шорт крупных трейдеров по ПОЗИЦИЯМ
    count_long_short_ratio                          — лонг/шорт всех счетов (толпа)
    sum_taker_long_short_vol_ratio                  — покупки/продажи тейкеров по объёму

В некоторые дни Binance не публикует доли (пустые поля) — они остаются NaN.
Результат: research/binance_metrics_cache/<ПАРА>.pkl (индекс — время UTC).

    python research/fetch_binance_metrics.py              # 20 пар списка бота, 2021-06 … вчера
    python research/fetch_binance_metrics.py BTCUSDT      # одна пара
"""
import csv
import io
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'binance_metrics_cache')
URL = 'https://data.binance.vision/data/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip'
START = os.getenv('BM_START', '2021-06-01')
BINANCE = {'SHIB1000USDT': '1000SHIBUSDT'}
COLS = ['sum_open_interest', 'sum_open_interest_value', 'count_toptrader_long_short_ratio',
        'sum_toptrader_long_short_ratio', 'count_long_short_ratio', 'sum_taker_long_short_vol_ratio']


def fetch_day(sym, day):
    url = URL.format(s=sym, d=day)
    for n in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}),
                                        timeout=40) as r:
                data = r.read()
            z = zipfile.ZipFile(io.BytesIO(data))
            rows = list(csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode())))
            return rows
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []                                   # пары ещё не было или день не выложен
            time.sleep(1 + n)
        except Exception:                                   # noqa: BLE001
            time.sleep(1 + n)
    return None                                             # не скачалось — отметим


def fetch_pair(pair):
    sym = BINANCE.get(pair, pair)
    path = os.path.join(OUT, pair + '.pkl')
    have = pd.read_pickle(path) if os.path.exists(path) else None
    days = pd.date_range(pd.Timestamp(START, tz='UTC'),
                         pd.Timestamp.now(tz='UTC').normalize() - pd.Timedelta(days=1), freq='D')
    done = set(have.index.normalize().unique()) if have is not None else set()
    todo = [d for d in days if d not in done]
    rows, failed = [], 0
    with ThreadPoolExecutor(8) as pool:
        for got in pool.map(lambda d: fetch_day(sym, d.strftime('%Y-%m-%d')), todo):
            if got is None:
                failed += 1
                continue
            rows += got
    if rows:
        f = pd.DataFrame(rows)
        f.index = pd.to_datetime(f['create_time'], utc=True)
        f = f[COLS].apply(pd.to_numeric, errors='coerce')
        if have is not None:
            f = pd.concat([have, f])
        f = f[~f.index.duplicated(keep='last')].sort_index()
        f.to_pickle(path + '.tmp')
        os.replace(path + '.tmp', path)
    else:
        f = have
    n = 0 if f is None else len(f)
    share = 0 if f is None or not n else f['count_toptrader_long_short_ratio'].notna().mean()
    print(f'  {pair:13s} дней скачано {len(todo) - failed:5d} из {len(todo):5d} (не скачалось {failed}); '
          f'строк {n:7d}; с долями {100 * share:5.1f}%', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    os.makedirs(OUT, exist_ok=True)
    sys.path.insert(0, HERE)
    import ai_doctrine as D
    for pair in sys.argv[1:] or D.PAIRS:
        fetch_pair(pair)
