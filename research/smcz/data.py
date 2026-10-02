"""Данные для SMC с нуля: минутки Binance и старшие ТФ, собранные из них.

Минутка — эталон исполнения. Старшие ТФ строятся по границам UTC
(ключ = минута // tf), поэтому бар 1ч начинается ровно в hh:00.
Время везде — целые минуты от эпохи (int64).
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_1M = os.path.join(ROOT, 'binance_klines_1m_cache')
CACHE = os.path.join(ROOT, 'backtest_cache_smcz')
FUNDING_DIR = os.path.join(ROOT, 'universe_cache', 'funding')

PAIRS = sorted(f[:-4] for f in os.listdir(SRC_1M) if f.endswith('.pkl'))

TF = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '2h': 120,
      '4h': 240, '6h': 360, '8h': 480, '12h': 720, '1d': 1440}


def minute(ts: str) -> int:
    return int(pd.Timestamp(ts, tz="UTC").value // 60_000_000_000)


PERIODS = {
    'DEV': (minute('2021-01-01'), minute('2024-01-01')),
    'VAL': (minute('2024-01-01'), minute('2025-04-01')),
    'HOLD': (minute('2025-04-01'), minute('2026-10-01')),
}


class Bars:
    """Свечи одного ТФ. t — начало бара (минута), i0/i1 — диапазон минуток."""

    __slots__ = ('tf', 't', 'o', 'h', 'l', 'c', 'qv', 'tbq', 'n', 'i0', 'i1')

    def __init__(self, tf, t, o, h, l, c, qv, tbq, n, i0, i1):
        self.tf = tf
        self.t, self.o, self.h, self.l, self.c = t, o, h, l, c
        self.qv, self.tbq, self.n, self.i0, self.i1 = qv, tbq, n, i0, i1

    def __len__(self):
        return len(self.t)

    @property
    def tclose(self):
        """Минута, с которой бар известен целиком (следующая после бара)."""
        return self.t + self.tf


BYBIT_DIR = os.path.join(CACHE, 'bybit')


def bybit_pairs():
    if not os.path.isdir(BYBIT_DIR):
        return []
    return sorted(f[:-7] for f in os.listdir(BYBIT_DIR) if f.endswith('_1m.npz'))


def load_1m(pair: str, src: str = 'bybit') -> Bars:
    """src='bybit' — основной набор (бот торгует там); 'binance' — сверка.
    У Bybit нет потока тейкеров: tbq — nan, а n — не число сделок, а метка
    дорисованной минуты (после resample — сколько таких минут в баре)."""
    if src == 'bybit':
        with np.load(os.path.join(BYBIT_DIR, f'{pair}_1m.npz')) as z:
            z = {k: z[k] for k in z.files}
        idx = np.arange(len(z['t']), dtype=np.int64)
        nan = np.full(len(idx), np.nan)
        return Bars(1, z['t'], z['o'], z['h'], z['l'], z['c'], z['qv'],
                    nan, z['filled'].astype(np.float64), idx, idx + 1)
    path = os.path.join(CACHE, f'{pair}_1m.npz')
    if os.path.exists(path):
        with np.load(path) as z:
            z = {k: z[k] for k in z.files}
        idx = np.arange(len(z['t']), dtype=np.int64)
        return Bars(1, z['t'], z['o'], z['h'], z['l'], z['c'], z['qv'],
                    z['tbq'], z['n'], idx, idx + 1)
    df = pd.read_pickle(os.path.join(SRC_1M, f'{pair}.pkl'))
    t = (df.index.as_unit("s").asi8 // 60).astype(np.int64)
    if np.any(np.diff(t) < 1):
        raise ValueError(f'{pair}: минутки не по порядку')
    # дыры (простой биржи) — плоские минуты по прошлому закрытию, объём 0
    full = np.arange(t[0], t[-1] + 1, dtype=np.int64)
    pos = t - t[0]
    arr = {}
    for k in ('o', 'h', 'l', 'c', 'qv', 'tbq', 'n'):
        a = np.full(len(full), np.nan if k in 'ohlc' else 0.0)
        a[pos] = df[k].to_numpy(np.float64)
        arr[k] = a
    c = pd.Series(arr['c']).ffill().to_numpy()
    for k in ('o', 'h', 'l'):
        arr[k] = np.where(np.isnan(arr[k]), c, arr[k])
    arr['c'] = c
    t = full
    idx = np.arange(len(t), dtype=np.int64)
    b = Bars(1, t, arr['o'], arr['h'], arr['l'], arr['c'], arr['qv'],
             arr['tbq'], arr['n'], idx, idx + 1)
    os.makedirs(CACHE, exist_ok=True)
    tmp = path[:-4] + f'.tmp{os.getpid()}.npz'
    np.savez(tmp, t=b.t, o=b.o, h=b.h, l=b.l, c=b.c, qv=b.qv, tbq=b.tbq,
             n=b.n)
    os.replace(tmp, path)
    return b


WEEK_OFFSET = 4 * 1440      # 1970-01-01 — четверг; неделя с понедельника


def resample(m1: Bars, tf: int, offset: int = 0) -> Bars:
    """Свечи ТФ tf (минуты) из базового ряда m1 (минутки или часы)."""
    if tf == m1.tf:
        return m1
    if tf % m1.tf:
        raise ValueError(f'ТФ {tf} не кратен базе {m1.tf}')
    key = (m1.t - offset) // tf
    # границы групп: минутки идут подряд, поэтому группы — непрерывные отрезки
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(key)]
    o = m1.o[starts]
    c = m1.c[ends - 1]
    h = np.maximum.reduceat(m1.h, starts)
    l = np.minimum.reduceat(m1.l, starts)
    qv = np.add.reduceat(m1.qv, starts)
    tbq = np.add.reduceat(m1.tbq, starts)
    n = np.add.reduceat(m1.n, starts)
    t = key[starts] * tf + offset
    # неполный первый бар (листинг посреди часа) и неполный последний — выбросить
    full = (ends - starts) == tf // m1.tf
    sel = np.flatnonzero(full)
    return Bars(tf, t[sel], o[sel], h[sel], l[sel], c[sel], qv[sel], tbq[sel],
                n[sel], starts[sel].astype(np.int64), ends[sel].astype(np.int64))


BV_DIR = os.path.join(CACHE, 'bv1h')


GAP_H = 48          # подряд дорисованных часов…
GAP_JUMP = 0.4      # …и скачок цены через дыру больше 40% — разрыв ряда
GAP_LONG_H = 7 * 24  # дыра от недели — разрыв ряда всегда
MIN_SEG_D = 120     # отрезок короче — не берётся


def _bv_raw(sym):
    with np.load(os.path.join(BV_DIR, f'{sym}.npz')) as z:
        return {k: z[k] for k in z.files}


def bv_segments(filled: np.ndarray, c: np.ndarray):
    """Отрезки ряда между разрывами. Снятый тикер бывает заново выдан другой
    монете, и склейка дала бы ложный скачок цены; а пропуски дней в самом
    архиве — не разрыв. Разрыв: дыра от недели или от 48 ч со скачком >40%."""
    f = filled.astype(bool)
    edges = np.flatnonzero(np.diff(np.r_[0, f.astype(np.int8), 0]))
    runs = edges.reshape(-1, 2)                      # [начало, конец) дорисованных
    cuts = []
    for a, b in runs:
        if b - a >= GAP_LONG_H:
            cuts.append((a, b))
        elif b - a >= GAP_H and a > 0 and b < len(c):
            if abs(np.log(c[b] / c[a - 1])) > np.log(1 + GAP_JUMP):
                cuts.append((a, b))
    segs, start = [], 0
    for a, b in cuts:
        segs.append((start, a))
        start = b
    segs.append((start, len(f)))
    return [(a, b) for a, b in segs if b - a >= MIN_SEG_D * 24]


def bv_pairs():
    """Имена вида SYM@k — k-й отрезок ряда символа."""
    out = []
    for f in sorted(os.listdir(BV_DIR)):
        if not f.endswith('.npz') or f.startswith('_') or '.tmp' in f:
            continue
        sym = f[:-4]
        z = _bv_raw(sym)
        for k, _ in enumerate(bv_segments(z['filled'], z['c'])):
            out.append(f'{sym}@{k}')
    return out


def load_bv1h(pair: str) -> Bars:
    """Часовой ряд широкой выборки (Binance Vision, с умершими монетами)."""
    sym, _, k = pair.partition('@')
    z = _bv_raw(sym)
    a, b = bv_segments(z['filled'], z['c'])[int(k or 0)]
    z = {key: v[a:b] for key, v in z.items()}
    idx = np.arange(len(z['t']), dtype=np.int64)
    return Bars(60, z['t'], z['o'], z['h'], z['l'], z['c'], z['qv'], z['tbq'],
                z['n'], idx, idx + 1)


def load(pair: str, tfs=('5m', '15m', '1h', '2h', '4h', '6h', '8h', '12h', '1d'),
         src='bybit') -> dict:
    """Свечи всех ТФ. 'base' — ряд исполнения (минутки; для bv1h — часы)."""
    base = load_bv1h(pair) if src == 'bv1h' else load_1m(pair, src)
    out = {'base': base, base_name(base): base}
    for name in tfs:
        if TF[name] >= base.tf:
            out[name] = resample(base, TF[name])
    out['1w'] = resample(base, 10080, WEEK_OFFSET)
    return out


def base_name(b: Bars) -> str:
    return {1: '1m', 60: '1h'}[b.tf]


def funding_cum(pair: str, src: str, t: np.ndarray):
    """Накопленная ставка фандинга на каждую минуту t (сумма ставок
    расчётов с минутой ≤ t). None — истории нет."""
    pair = pair.partition('@')[0]
    path = os.path.join(CACHE, f'funding_{src}', f'{pair}.npz')
    if not os.path.exists(path):
        return None
    with np.load(path) as z:
        ft, rate = z['t'], z['rate']
    cs = np.r_[0.0, np.cumsum(rate)]
    return cs[np.searchsorted(ft, t, side='right')]


def period_of(t_minute: np.ndarray) -> np.ndarray:
    out = np.full(len(t_minute), '', dtype=object)
    for name, (a, b) in PERIODS.items():
        out[(t_minute >= a) & (t_minute < b)] = name
    return out
