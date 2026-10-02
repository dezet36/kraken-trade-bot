"""Широкая выборка без выживших: часовые свечи Binance USDT-M из архива
data.binance.vision — все контракты, включая снятые с торгов (LUNA, FTT…).

Пишет research/backtest_cache_smcz/bv1h/{SYM}.npz: t (минута начала часа),
o h l c, qv (оборот USDT), tbq (покупки тейкеров), n (сделки), filled
(1 — час дорисован плоским). Возобновляемо.

    python -m smcz.fetch_bvision list     # список символов → bv1h/_symbols.txt
    python -m smcz.fetch_bvision fetch    # свечи по списку
"""
from __future__ import annotations

import io
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

from smcz.data import CACHE

S3 = 'https://s3-ap-northeast-1.amazonaws.com/data.binance.vision'
DL = 'https://data.binance.vision/'
OUT = os.path.join(CACHE, 'bv1h')
THREADS = 48
# индексы и не-крипто: не монеты
EXCLUDE = {'BTCDOMUSDT', 'DEFIUSDT', 'FOOTBALLUSDT', 'BLUEBIRDUSDT', 'USDCUSDT',
           'BTCSTUSDT'}
S = requests.Session()


def _list(prefix):
    keys, prefixes, marker = [], [], ''
    while True:
        for attempt in range(6):
            try:
                r = S.get(S3, params={'delimiter': '/', 'prefix': prefix, 'marker': marker}, timeout=30)
                break
            except requests.RequestException:
                time.sleep(1 + attempt)
        k = re.findall(r'<Key>([^<]+)</Key>', r.text)
        p = re.findall(r'<Prefix>([^<]+)</Prefix>', r.text)
        keys += k
        prefixes += [x for x in p if x != prefix]
        if '<IsTruncated>true' in r.text:
            marker = (k + p)[-1]
        else:
            return keys, prefixes


def cmd_list():
    _, pre = _list('data/futures/um/monthly/klines/')
    syms = [p.split('/')[-2] for p in pre]
    syms = [s for s in syms if s.endswith('USDT') and s not in EXCLUDE]
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, '_symbols.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(syms))
    print(len(syms), 'символов')


def _zip_rows(key):
    for attempt in range(6):
        try:
            r = S.get(DL + key, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            break
        except requests.RequestException:
            time.sleep(1 + 2 * attempt)
    else:
        raise RuntimeError(key)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    rows = []
    for ln in raw.splitlines():
        if not ln or not ln[0].isdigit():
            continue                     # заголовок
        p = ln.split(',')
        rows.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]),
                     float(p[7]), float(p[10]), float(p[8])))
    return rows


def fetch_sym(sym):
    path = os.path.join(OUT, f'{sym}.npz')
    if os.path.exists(path):
        return sym, 'есть'
    keys, _ = _list(f'data/futures/um/monthly/klines/{sym}/1h/')
    keys = sorted(k for k in keys if k.endswith('.zip'))
    if not keys:
        return sym, 'нет файлов'
    first = re.search(r'(\d{4})-(\d{2})\.zip$', keys[0])
    if first and (int(first.group(1)), int(first.group(2))) >= (2025, 4):
        return sym, 'поздний листинг'
    rows = []
    for k in keys:
        rr = _zip_rows(k)
        if rr:
            rows += rr
    if not rows:
        return sym, 'пусто'
    a = np.array(sorted(set(rows)))
    t = (a[:, 0] // 60_000).astype(np.int64)
    t, u = np.unique(t, return_index=True)
    a = a[u]
    full = np.arange(t[0], t[-1] + 60, 60, dtype=np.int64)
    pos = (t - t[0]) // 60
    cols = {}
    for j, name in enumerate(('o', 'h', 'l', 'c', 'qv', 'tbq', 'n'), start=1):
        x = np.full(len(full), np.nan if name in ('o', 'h', 'l', 'c') else 0.0)
        x[pos] = a[:, j]
        cols[name] = x
    filled = np.isnan(cols['c'])
    for i in np.flatnonzero(filled):
        p = cols['c'][i - 1]
        cols['o'][i] = cols['h'][i] = cols['l'][i] = cols['c'][i] = p
    tmp = path[:-4] + '.tmp.npz'
    np.savez(tmp, t=full, filled=filled.astype(np.int8), **cols)
    os.replace(tmp, path)
    return sym, f'{len(full)} ч с {np.datetime64(int(full[0]), "m")}'


def cmd_fetch():
    with open(os.path.join(OUT, '_symbols.txt'), encoding='utf-8') as f:
        syms = [s.strip() for s in f if s.strip()]
    with ThreadPoolExecutor(THREADS) as ex:
        for sym, msg in ex.map(fetch_sym, syms):
            print(sym, msg, flush=True)


if __name__ == '__main__':
    {'list': cmd_list, 'fetch': cmd_fetch}[sys.argv[1]]()
