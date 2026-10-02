"""История фандинга: Binance USDT-M и Bybit linear, через публичные API.

Пишет research/backtest_cache_smcz/funding_{binance|bybit}/{SYM}.npz:
t — минута расчёта (int64), rate — ставка за период (доля, + — платят
покупатели). Возобновляемо: готовые символы пропускаются.

    python -m smcz.fetch_funding binance SYM ...
    python -m smcz.fetch_funding bybit SYM ...
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import requests

from smcz.data import CACHE, minute

START_MS = minute('2020-01-01') * 60_000
END_MS = minute('2026-10-01') * 60_000
S = requests.Session()


def _get(url, params):
    for attempt in range(8):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + 2 * attempt)
                continue
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(1 + 2 * attempt)
    raise RuntimeError(f'{url} {params}: нет ответа')


_last = [0.0]


def _pace(min_gap=0.7):
    """fundingRate у Binance — 500 запросов за 5 минут на IP: держим ~85/мин."""
    w = _last[0] + min_gap - time.time()
    if w > 0:
        time.sleep(w)
    _last[0] = time.time()


def binance(sym):
    out, t = [], START_MS
    while t < END_MS:
        _pace()
        j = _get('https://fapi.binance.com/fapi/v1/fundingRate',
                 dict(symbol=sym, startTime=t, endTime=END_MS, limit=1000))
        if not isinstance(j, list):
            raise RuntimeError(f'{sym}: {j}')
        if not j:
            break
        out += [(int(x['fundingTime']), float(x['fundingRate'])) for x in j]
        t = int(j[-1]['fundingTime']) + 1
        if len(j) < 1000:
            break
    return out


def bybit(sym):
    out, end = [], END_MS
    while end > START_MS:
        j = _get('https://api.bybit.com/v5/market/funding/history',
                 dict(category='linear', symbol=sym, startTime=START_MS, endTime=end, limit=200))
        rows = j.get('result', {}).get('list', []) if j.get('retCode') == 0 else None
        if rows is None:
            raise RuntimeError(f'{sym}: {j.get("retCode")} {j.get("retMsg")}')
        if not rows:
            break
        out += [(int(x['fundingRateTimestamp']), float(x['fundingRate'])) for x in rows]
        end = min(int(x['fundingRateTimestamp']) for x in rows) - 1
        if len(rows) < 200:
            break
    return out


def main(argv):
    src, syms = argv[0], argv[1:]
    if syms == ['@bv']:                    # вся широкая выборка
        from smcz.data import BV_DIR
        with open(os.path.join(BV_DIR, '_symbols.txt'), encoding='utf-8') as f:
            syms = [x.strip() for x in f if x.strip()]
    folder = os.path.join(CACHE, f'funding_{src}')
    os.makedirs(folder, exist_ok=True)
    fn = binance if src == 'binance' else bybit
    for sym in syms:
        path = os.path.join(folder, f'{sym}.npz')
        if os.path.exists(path):
            continue
        try:
            rows = fn(sym)
        except Exception as e:      # один символ не валит остальные
            print(sym, 'ОШИБКА', e, flush=True)
            continue
        if not rows:
            print(sym, 'пусто', flush=True)
            continue
        a = np.array(sorted(set(rows)))
        t = (a[:, 0] // 60_000).astype(np.int64)
        tmp = path[:-4] + '.tmp.npz'
        np.savez(tmp, t=t, rate=a[:, 1])
        os.replace(tmp, path)
        print(sym, len(t), np.datetime64(int(t[0]), 'm'), f'среднее {a[:, 1].mean() * 1e4:.2f} б.п.', flush=True)


if __name__ == '__main__':
    main(sys.argv[1:])
