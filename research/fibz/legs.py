"""ФИБО с нуля: импульсы (ноги A → B) и заявки на откат — research/fibz/PROTOCOL.md.

Нога покупки: свинг-минимум A → свинг-максимум B. A — минимум между прошлым
подтверждённым свинг-максимумом и B; B — свинг-максимум (фрактал k), он же
максимум ноги. Нога известна на закрытии бара B+k. Продажа — зеркально.
Всё, что видит событие, закрыто к этому моменту.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numba import njit

from smcz import structure as S
from smcz.data import Bars
from smcz.events import _htf_trend


@njit(cache=True)
def _legs(h, l, ph, pl, k):
    """(dir, a, b, conf, A, B, prev_same) по каждой ноге. prev_same — цена
    прошлого свинга той же стороны, что B (для «продолжение/противоход»)."""
    n = len(h)
    out = []
    last_hi = -1          # индекс последнего подтверждённого свинг-максимума
    last_lo = -1
    prev_hi_px = np.nan
    prev_lo_px = np.nan
    for c in range(n):
        j = c - k
        if j < 0:
            continue
        if ph[j]:
            # бычья нога: A — минимум между прошлым свинг-максимумом и j
            s = last_hi + 1 if last_hi >= 0 else max(0, j - 200)
            if s < j:
                a = s
                for m in range(s, j):
                    if l[m] < l[a]:
                        a = m
                ok = True
                for m in range(a, j):
                    if h[m] > h[j]:
                        ok = False
                        break
                if ok and h[j] > l[a]:
                    out.append((1, a, j, c, l[a], h[j], prev_hi_px))
            last_hi = j
            prev_hi_px = h[j]
        if pl[j]:
            s = last_lo + 1 if last_lo >= 0 else max(0, j - 200)
            if s < j:
                a = s
                for m in range(s, j):
                    if h[m] > h[a]:
                        a = m
                ok = True
                for m in range(a, j):
                    if l[m] < l[j]:
                        ok = False
                        break
                if ok and h[a] > l[j]:
                    out.append((-1, a, j, c, h[a], l[j], prev_lo_px))
            last_lo = j
            prev_lo_px = l[j]
    return out


@njit(cache=True)
def _first_beyond(h1, l1, start, limit, px, d):
    """Первая минутка в [start, limit), где цена ушла за px по ходу ноги
    (выше для покупки, ниже для продажи); limit — если не ушла."""
    n = len(px)
    out = np.empty(n, np.int64)
    N = len(h1)
    for q in range(n):
        e = min(limit[q], N)
        r = e
        for i in range(start[q], e):
            if (d[q] == 1 and h1[i] > px[q]) or (d[q] == -1 and l1[i] < px[q]):
                r = i
                break
        out[q] = r
    return out


def build(bars: dict, tf: str, k: int, expiry_bars: int) -> pd.DataFrame:
    B = bars[tf]
    m1 = bars['base']
    ph, pl = S.pivots(B.h, B.l, k)
    raw = _legs(B.h, B.l, ph, pl, k)
    if not raw:
        return pd.DataFrame()
    arr = np.array(raw, dtype=np.float64)
    d = arr[:, 0].astype(np.int64)
    a, b, c = (arr[:, i].astype(np.int64) for i in (1, 2, 3))
    A, Bp, prev = arr[:, 4], arr[:, 5], arr[:, 6]
    atr = S.atr(B.h, B.l, B.c, 14)
    i_place = B.i1[c]
    per = B.tf // m1.tf
    limit = i_place + expiry_bars * per
    i_beyond = _first_beyond(m1.h, m1.l, i_place, limit, Bp, d)
    df = pd.DataFrame({
        'dir': d, 'a': a, 'b': b, 'conf': c, 'A': A, 'B': Bp,
        't': B.tclose[c], 'i_place': i_place, 'i_cancel': i_beyond,
        'close': B.c[c], 'atr': atr[c],
    })
    L = np.abs(Bp - A)
    df['size_atr'] = L / atr[c]
    df['size_pct'] = L / B.c[c]
    df['leg_bars'] = b - a
    # откат к подтверждению: какая доля ноги уже отдана
    df['retr_conf'] = (Bp - B.c[c]) * d / L
    # продолжение: B за прошлым свингом той же стороны (выше максимум / ниже минимум)
    df['cont'] = np.where(np.isnan(prev), 0, np.where((Bp - prev) * d > 0, 1, -1))
    for name, kk in (('4h', 3), ('1d', 2), ('1w', 2)):
        if bars[name].tf > B.tf:
            df[f'tr_{name}'] = _htf_trend(B, bars[name], kk)[c] * d
        else:
            df[f'tr_{name}'] = 0
    qv = pd.Series(B.qv)
    vma = qv.rolling(50, min_periods=10).mean().shift(1).to_numpy()
    cq = np.r_[0.0, np.cumsum(B.qv)]
    df['vol_leg'] = (cq[b + 1] - cq[a]) / np.maximum(b - a + 1, 1) / vma[c]
    if not np.all(np.isnan(B.tbq)):
        ct = np.r_[0.0, np.cumsum(B.tbq)]
        buy = (ct[b + 1] - ct[a]) / np.maximum(cq[b + 1] - cq[a], 1e-9)
        df['taker_leg'] = np.where(d == 1, buy, 1 - buy)     # доля агрессора по ходу ноги
    else:
        df['taker_leg'] = np.nan
    df['hour'] = (B.tclose[c] % 1440) // 60
    return df[np.isfinite(df.atr) & (L > 0)].reset_index(drop=True)


def orders(df: pd.DataFrame, B: Bars, unit: int, r: float, stop: str, target, expiry_bars: int,
           hold_bars: int) -> dict:
    """Заявки на откат r. stop: 'A' — за началом ноги, '786' — за уровнем 0.786;
    оба + 0.1·ATR. target: 'B', 'e1272', 'e1618' или кратное R."""
    d = df.dir.to_numpy()
    A, Bp, atr = df.A.to_numpy(), df.B.to_numpy(), df.atr.to_numpy()
    L = (Bp - A) * d
    entry = Bp - d * r * L
    if stop == 'A':
        st = A - d * 0.1 * atr
    else:
        st = Bp - d * 0.786 * L - d * 0.1 * atr
    risk = (entry - st) * d
    if target == 'B':
        tg = Bp.copy()
    elif target == 'e1272':
        tg = Bp + d * 0.272 * L
    elif target == 'e1618':
        tg = Bp + d * 0.618 * L
    else:
        tg = entry + d * float(target) * risk
    per = B.tf // unit
    i_place = df.i_place.to_numpy()
    i_expire = np.minimum(i_place + expiry_bars * per, df.i_cancel.to_numpy())
    hold = np.minimum(hold_bars * per, 30 * 1440 // unit)
    # к подтверждению цена уже за уровнем входа — сетапа нет (протокол)
    valid = ((df.close.to_numpy() - entry) * d > 0) & (risk > 0) & ((tg - entry) * d > 0)
    return dict(side=d, i_place=i_place, i_expire=i_expire, entry=entry, stop=st, target=tg,
                hold=np.full(len(df), hold, np.int64), ev=np.arange(len(df)), ref=entry, risk=risk,
                valid=valid)
