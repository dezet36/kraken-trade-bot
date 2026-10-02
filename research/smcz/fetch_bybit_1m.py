"""Минутки Bybit (линейные перпетуалы) через публичный API v5.

Основной набор для smcz: бот торгует на Bybit и берёт свечи оттуда.
Ответ /v5/market/kline: [start, open, high, low, close, volume, turnover],
новые сверху, до 1000 баров. Потока тейкеров в нём нет.

Пишет research/backtest_cache_smcz/bybit/{PAIR}_1m.npz: t (минута), o h l c,
v (объём в монетах), qv (оборот в USDT), filled (1 — минута дорисована
плоской: биржа её не отдала). Возобновляемо: готовые пары пропускаются.

    python -m smcz.fetch_bybit_1m [PAIR ...]
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from smcz.data import CACHE, PAIRS, minute

OUT = os.path.join(CACHE, 'bybit')
URL = 'https://api.bybit.com/v5/market/kline'
START = minute('2021-01-01')
END = minute('2026-10-01')
EXTRA = ['BICOUSDT']           # есть в пуле бота, нет в наборе Binance
THREADS = 8

_session = requests.Session()


def _get(symbol, start_min, limit=1000):
    params = dict(category='linear', symbol=symbol, interval='1',
                  start=start_min * 60_000, end=(start_min + limit - 1) * 60_000,
                  limit=limit)
    for attempt in range(8):
        try:
            r = _session.get(URL, params=params, timeout=20)
            j = r.json()
            if j.get('retCode') == 0:
                return j['result']['list']
            if j.get("retCode") in (10006, 10016, 10018):   # лимит частоты, сбой сервиса
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(f'{symbol} {start_min}: {j.get("retCode")} {j.get("retMsg")}')
        except (requests.RequestException, ValueError):
            time.sleep(1 + attempt * 2)
    raise RuntimeError(f'{symbol} {start_min}: нет ответа')


def first_minute(symbol):
    """Первая минута истории не раньше START (листинг может быть позже).
    Запрос только со start отдаёт первые 1000 баров с этой минуты или с
    листинга; запрос со start и end по дням до листинга приходит пустым."""
    for attempt in range(8):
        try:
            j = _session.get(URL, params=dict(category='linear', symbol=symbol,
                                              interval='1', start=START * 60_000,
                                              limit=1000), timeout=20).json()
            break
        except (requests.RequestException, ValueError):
            time.sleep(1 + attempt * 2)
    rows = j['result']['list'] if j.get('retCode') == 0 else []
    if not rows:
        return None
    return min(int(r[0]) // 60_000 for r in rows)


def fetch_pair(symbol):
    path = os.path.join(OUT, f'{symbol}_1m.npz')
    if os.path.exists(path):
        print(symbol, 'уже есть', flush=True)
        return
    t0 = time.time()
    first = first_minute(symbol)
    if first is None:
        print(symbol, 'нет на Bybit', flush=True)
        return
    starts = list(range(first, END, 1000))
    with ThreadPoolExecutor(THREADS) as ex:
        chunks = list(ex.map(lambda s: _get(symbol, s), starts))
    rows = [r for ch in chunks for r in ch]
    if not rows:
        print(symbol, 'пусто в окне', flush=True)
        return
    arr = np.array([[float(x) for x in r[:7]] for r in rows])
    t = (arr[:, 0] // 60_000).astype(np.int64)
    order = np.argsort(t)
    t, arr = t[order], arr[order]
    t, uniq = np.unique(t, return_index=True)
    arr = arr[uniq]
    # дорисовать пропуски плоскими минутами (цена = прошлое закрытие)
    full = np.arange(t[0], t[-1] + 1, dtype=np.int64)
    pos = t - t[0]
    o = np.full(len(full), np.nan)
    h, l, c = o.copy(), o.copy(), o.copy()
    v = np.zeros(len(full))
    qv = np.zeros(len(full))
    o[pos], h[pos], l[pos], c[pos] = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]
    v[pos], qv[pos] = arr[:, 5], arr[:, 6]
    filled = np.isnan(c)
    for i in np.flatnonzero(filled):
        p = c[i - 1]
        o[i] = h[i] = l[i] = c[i] = p
    os.makedirs(OUT, exist_ok=True)
    tmp = path + '.tmp.npz'
    np.savez(tmp, t=full, o=o, h=h, l=l, c=c, v=v, qv=qv, filled=filled.astype(np.int8))
    os.replace(tmp, path)
    print(f'{symbol}: {len(full)} мин с {np.datetime64(int(full[0]), "m")}, '
          f'дорисовано {int(filled.sum())}, {time.time() - t0:.0f} с', flush=True)


def main(argv):
    pairs = argv or (PAIRS + EXTRA)
    for p in pairs:
        try:
            fetch_pair(p)
        except Exception as e:      # одна пара не должна валить остальные
            print(p, 'ОШИБКА', e, flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
