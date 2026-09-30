"""
Часовой набор для поиска закономерностей ИИ (27.09.2026): по каждой паре бота
одна таблица с 2022-01-01 — цена и поток, позиционирование толпы, ОИ, фандинг.

Новое против прежних замеров (там были только цена, ОИ, фандинг, премия):
    tb, tbq      — объём покупок ПО РЫНКУ (taker buy) из свечей Binance:
                   дельта агрессора с историей, которой у Bybit нет;
    trades       — число сделок в часе (Binance);
    buy_ratio    — доля счетов в лонге на Bybit (account-ratio, 1 ч).
Плюс ОИ (Bybit, 1 ч) и фандинг (Bybit, выплаты раз в 8 ч, протянут вперёд
с момента выплаты — будущего в строке нет).

    python research/ai_flow_fetch.py            # все пары, докачка
    python research/ai_flow_fetch.py BTCUSDT    # одна пара
Результат: research/flow_cache/<пара>.pkl
"""
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.getenv('FLOW_CACHE', 'flow_cache'))
# Период — 2022-01-01 … сейчас; FLOW_START/FLOW_END — другой (п. 75: 2021 год, в свою папку FLOW_CACHE).
START = int(pd.Timestamp(os.getenv('FLOW_START', '2022-01-01'), tz='UTC').timestamp() * 1000)
END = os.getenv('FLOW_END')
H = 3_600_000

PAIRS = ('AAVEUSDT', 'ADAUSDT', 'ARBUSDT', 'AVAXUSDT', 'BNBUSDT', 'BTCUSDT', 'COTIUSDT',
         'DOGEUSDT', 'DOTUSDT', 'ETHUSDT', 'LINKUSDT', 'LTCUSDT', 'NEARUSDT', 'SHIB1000USDT',
         'SOLUSDT', 'SUIUSDT', 'UNIUSDT', 'XLMUSDT', 'XRPUSDT', 'ZECUSDT')
BINANCE = {'SHIB1000USDT': '1000SHIBUSDT'}


def get(url, tries=6):
    for n in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except Exception as exc:                       # noqa: BLE001
            if n == tries - 1:
                raise
            time.sleep(1.5 * (n + 1))
            last = exc                                 # noqa: F841


def binance_klines(pair, now):
    sym = BINANCE.get(pair, pair)
    rows, start = [], START
    while start < now:
        batch = get(f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1h&startTime={start}&limit=1500')
        if not batch:
            break
        rows += [b for b in batch if b[0] <= now]
        start = batch[-1][0] + H
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'close_ts', 'qv', 'trades', 'tb', 'tbq', 'ignore'])
    df = df.drop(columns=['close_ts', 'ignore']).drop_duplicates('ts')
    for col in ('o', 'h', 'l', 'c', 'v', 'qv', 'tb', 'tbq'):
        df[col] = df[col].astype(float)
    df['trades'] = df['trades'].astype(int)
    return df.set_index('ts')


def bybit_ratio(pair, now):
    rows, start = [], START
    while start < now:
        end = start + 500 * H
        r = get(f'https://api.bybit.com/v5/market/account-ratio?category=linear&symbol={pair}&period=1h'
                f'&limit=500&startTime={start}&endTime={end}')
        rows += (r.get('result') or {}).get('list') or []
        start = end
        time.sleep(0.12)
    if not rows:
        return pd.Series(dtype=float, name='buy_ratio')
    s = pd.Series({int(x['timestamp']): float(x['buyRatio']) for x in rows}, name='buy_ratio')
    return s.sort_index()


def bybit_backwards(url_of, key_ts, key_val, now, stop_at=START):
    """Шагом endTime назад: у Bybit ОИ и фандинг берутся так (память exchange-history-depth)."""
    out, end = {}, now
    while end > stop_at:
        r = get(url_of(end))
        batch = (r.get('result') or {}).get('list') or []
        if not batch:
            break
        for x in batch:
            out[int(x[key_ts])] = float(x[key_val])
        oldest = min(int(x[key_ts]) for x in batch)
        if oldest >= end:
            break
        end = oldest - 1
        time.sleep(0.12)
    return pd.Series(out).sort_index()


def fetch(pair):
    path = os.path.join(OUT, f'{pair}.pkl')
    if os.path.exists(path) and len(sys.argv) < 2:
        return pair, 'есть'
    now = (int(pd.Timestamp(END, tz='UTC').timestamp() * 1000) if END
           else int(time.time() * 1000) // H * H)
    k = binance_klines(pair, now)
    ratio = bybit_ratio(pair, now)
    oi = bybit_backwards(lambda end: f'https://api.bybit.com/v5/market/open-interest?category=linear&symbol={pair}'
                                     f'&intervalTime=1h&limit=200&endTime={end}', 'timestamp', 'openInterest', now)
    fr = bybit_backwards(lambda end: f'https://api.bybit.com/v5/market/funding/history?category=linear&symbol={pair}'
                                     f'&limit=200&endTime={end}', 'fundingRateTimestamp', 'fundingRate', now)
    df = k.copy()
    df['buy_ratio'] = ratio.reindex(df.index)
    df['oi'] = oi.reindex(df.index)
    # Фандинг выплачен в метку выплаты; в часе с этой меткой и дальше он известен.
    df['funding'] = fr.reindex(df.index, method='ffill') if len(fr) else float('nan')
    df.index = pd.to_datetime(df.index, unit='ms', utc=True)
    tmp = path + '.tmp'
    df.to_pickle(tmp)
    os.replace(tmp, path)
    if df.empty:                                       # пары в этом периоде ещё не было
        return pair, '0 ч — пары в периоде нет'
    return pair, f'{len(df)} ч, {df.index.min():%Y-%m-%d}…{df.index.max():%Y-%m-%d}, ratio {df.buy_ratio.notna().mean():.0%}, oi {df.oi.notna().mean():.0%}'


def main():
    os.makedirs(OUT, exist_ok=True)
    pairs = [p for p in sys.argv[1:]] or list(PAIRS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for pair, msg in pool.map(fetch, pairs):
            print(pair, msg, flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
