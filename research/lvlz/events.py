"""Уровни с нуля: уровни, проколы с возвратом и заявки — research/lvlz/PROTOCOL.md.

Всё на закрытых барах рабочего ТФ: уровень известен с бара `known` (свинг —
после подтверждения k барами справа; день/неделя — после их закрытия),
сигнал — на закрытии бара возврата. Ни одна проверка не смотрит правее
бара события.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numba import njit

from smcz import structure as S
from smcz.data import Bars, WEEK_OFFSET
from smcz.events import _htf_trend

W = 100            # сколько баров живёт уровень свинга/кластера
Q = 2              # возврат — в баре прокола или за Q баров после него
CL_TOL_ATR = 0.25  # касания кластера — в пределах 0.25·ATR
MIN_STOP = 0.003   # стоп теснее 0.3% цены — сетапа нет (протокол)


# ── Уровни ──────────────────────────────────────────────────────────────────

def levels_sw(B: Bars, k: int):
    """Свинг-минимум — поддержка (+1), свинг-максимум — сопротивление (−1)."""
    ph, pl = S.pivots(B.h, B.l, k)
    n = len(B)
    rows = []
    for j in np.flatnonzero(pl):
        if j + k < n:
            rows.append((B.l[j], j + k, min(j + k + W, n - 1), 1, np.nan))
    for j in np.flatnonzero(ph):
        if j + k < n:
            rows.append((B.h[j], j + k, min(j + k + W, n - 1), -1, np.nan))
    return rows


def levels_cl(B: Bars, k: int, atr: np.ndarray):
    """Кластер двух свингов любого вида в пределах 0.25·ATR и W баров; обе стороны."""
    ph, pl = S.pivots(B.h, B.l, k)
    n = len(B)
    pts = sorted([(j, B.h[j]) for j in np.flatnonzero(ph)] + [(j, B.l[j]) for j in np.flatnonzero(pl)])
    rows = []
    for b in range(len(pts)):
        jb, pb = pts[b]
        known = jb + k
        if known >= n or not np.isfinite(atr[known]):
            continue
        tol = CL_TOL_ATR * atr[known]
        best = None
        for a in range(b - 1, -1, -1):
            ja, pa = pts[a]
            if jb - ja > W:
                break
            if jb - ja < 2 * k + 1:           # то же место — не второе касание
                continue
            if abs(pa - pb) <= tol and (best is None or abs(pa - pb) < abs(best - pb)):
                best = pa
        if best is not None:
            rows.append(((best + pb) / 2, known, min(known + W, n - 1), 0, np.nan))
    return rows


def _period_levels(B: Bars, P: Bars, offset: int):
    """Максимум/минимум прошлого периода P (день, неделя) — живут следующий период.
    Возвращает уровни с противоположной границей как целью opp."""
    pid = (B.t - offset) // P.tf
    rows = []
    n = len(B)
    first = {}
    last = {}
    for i in range(n):
        first.setdefault(pid[i], i)
        last[pid[i]] = i
    phi = {(int(t) - offset) // P.tf: (h, l) for t, h, l in zip(P.t, P.h, P.l)}
    for cur, i0 in first.items():
        prev = phi.get(cur - 1)
        if prev is None or i0 == 0:
            continue
        hi, lo = prev
        known, expire = i0 - 1, last[cur]
        rows.append((lo, known, expire, 1, hi))     # поддержка, цель — максимум
        rows.append((hi, known, expire, -1, lo))    # сопротивление, цель — минимум
    return rows


def levels_pd(B: Bars, D1: Bars):
    return _period_levels(B, D1, 0)


def levels_pw(B: Bars, W1: Bars):
    return _period_levels(B, W1, WEEK_OFFSET)


# ── Проколы с возвратом ─────────────────────────────────────────────────────

@njit(cache=True)
def _scan(h, l, c, atr, P, known, expire, side, p, q):
    """События по уровням. side: +1 — только покупка, −1 — только продажа, 0 — обе.
    -> (уровень, бар события, сторона, экстремум прокола, первый бар прокола)."""
    out = []
    for z in range(len(P)):
        lv = P[z]
        for d in (1, -1):
            if side[z] != 0 and side[z] != d:
                continue
            beyond = -1                     # первый бар закрытия за уровнем
            for i in range(known[z] + 1, expire[z] + 1):
                a = atr[i]
                if not np.isfinite(a) or a <= 0:
                    continue
                if (c[i] - lv) * d < 0:     # закрытие за уровнем
                    if beyond < 0:
                        if (c[i - 1] - lv) * d <= 0:
                            break           # пришли не со своей стороны — уровень не наш
                        beyond = i
                    elif i - beyond >= q:
                        break               # не вернулись за q баров — уровень сломан
                    continue
                s = beyond if beyond >= 0 else i
                if beyond < 0 and (c[i - 1] - lv) * d <= 0:
                    continue                # бар не из-за уровня — ждём подхода
                ext = lv
                for m in range(s, i + 1):
                    v = l[m] if d == 1 else h[m]
                    if (v - ext) * d < 0:
                        ext = v
                if (lv - ext) * d >= p * a:
                    out.append((z, i, d, ext, s))
                    break                   # один сигнал на уровень и сторону
                beyond = -1
    return out


def build(bars: dict, tf: str, src: str, k: int, p: float) -> pd.DataFrame:
    B = bars[tf]
    atr = S.atr(B.h, B.l, B.c, 14)
    if src == 'sw':
        rows = levels_sw(B, k)
    elif src == 'cl':
        rows = levels_cl(B, k, atr)
    elif src == 'pd':
        rows = levels_pd(B, bars['1d'])
    elif src == 'pw':
        rows = levels_pw(B, bars['1w'])
    else:
        raise ValueError(src)
    if not rows:
        return pd.DataFrame()
    arr = np.array(rows, dtype=np.float64)
    ev = _scan(B.h, B.l, B.c, atr, arr[:, 0], arr[:, 1].astype(np.int64), arr[:, 2].astype(np.int64),
               arr[:, 3].astype(np.int64), float(p), Q)
    if not ev:
        return pd.DataFrame()
    e = np.array(ev, dtype=np.float64)
    z, i, d = e[:, 0].astype(np.int64), e[:, 1].astype(np.int64), e[:, 2].astype(np.int64)
    m1 = bars['base']
    df = pd.DataFrame({'dir': d, 'i': i, 'level': arr[z, 0], 'opp': arr[z, 4], 'ext': e[:, 3],
                       'pierce_bar': e[:, 4].astype(np.int64), 'known': arr[z, 1].astype(np.int64),
                       't': B.tclose[i], 'i_place': B.i1[i], 'close': B.c[i], 'atr': atr[i]})
    df['depth_atr'] = (df.level - df.ext) * df.dir / df.atr
    df['age'] = df.i - df.known
    qv = pd.Series(B.qv)
    vma = qv.rolling(50, min_periods=10).mean().shift(1).to_numpy()
    df['vol_ratio'] = B.qv[i] / vma[i]
    for name, kk in (('4h', 3), ('1d', 2), ('1w', 2)):
        if bars[name].tf > B.tf:
            df[f'tr_{name}'] = _htf_trend(B, bars[name], kk)[i] * d
        else:
            df[f'tr_{name}'] = 0
    df['hour'] = (B.tclose[i] % 1440) // 60
    D1 = bars['1d']
    adv = pd.Series(D1.qv, index=D1.t // 1440).rolling(30, min_periods=20).mean()
    df['adv30'] = adv.reindex(B.tclose[i] // 1440 - 1).to_numpy()
    df['age_d'] = (B.tclose[i] - m1.t[0]) / 1440.0
    df = df.sort_values('i', kind='stable').reset_index(drop=True)
    return df[np.isfinite(df.atr)].reset_index(drop=True)


def orders(df: pd.DataFrame, B: Bars, unit: int, entry: str, target, hold_bars: int,
           expiry_bars: int = 6) -> dict:
    """Заявки. entry: 'mkt' — по рынку после события; 'lvl' — лимит на уровне.
    target: кратное R, 'opp' (противоположная граница дня/недели) или 'none'."""
    d = df.dir.to_numpy()
    lvl = df.level.to_numpy()
    stop = df.ext.to_numpy() - d * 0.1 * df.atr.to_numpy()
    if entry == 'mkt':
        ent = np.full(len(df), np.nan)
        ref = df.close.to_numpy()
    else:
        ent = lvl.copy()
        ref = lvl.copy()
    risk = (ref - stop) * d
    if target == 'none':
        tg = np.where(d == 1, np.inf, -np.inf)
    elif target == 'opp':
        tg = df.opp.to_numpy()
    else:
        tg = ref + d * float(target) * risk
    per = B.tf // unit
    i_place = df.i_place.to_numpy()
    i_expire = i_place + (1 if entry == 'mkt' else expiry_bars * per)
    hold = np.minimum(hold_bars * per, 30 * 1440 // unit)
    valid = (risk > 0) & (risk / ref >= MIN_STOP) & np.isfinite(tg if target != 'none' else ref) \
        & ((tg - ref) * d > 0)
    return dict(side=d, i_place=i_place, i_expire=i_expire, entry=ent, stop=stop, target=tg,
                hold=np.full(len(df), hold, np.int64), ev=np.arange(len(df)), ref=ref, risk=risk,
                valid=valid)
