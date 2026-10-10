"""Зоны вместо уровня — дополнение протокола 10.10.2026 (research/lvlz/PROTOCOL.md).

Z1 — зона вокруг уровня (свинг/кластер), Z2 — база перед импульсом.
Вход `conf` — заход в зону и закрытие обратно за ближнюю границу; `edge` —
лимит на ближней границе. Всё по закрытым барам; зона известна с бара `known`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numba import njit

from smcz import structure as S
from smcz.data import Bars
from lvlz import events as E

W = 100
RET = 2            # закрытие обратно за ближнюю границу — в баре захода или за 2 бара


def zones_z1(B: Bars, src: str, k: int, w: float, atr):
    rows = E.levels_sw(B, k) if src == 'sw' else E.levels_cl(B, k, atr)
    out = []
    for P, known, expire, side, _ in rows:
        a = atr[int(known)]
        if not np.isfinite(a):
            continue
        out.append((P + w * a, P - w * a, int(known), int(expire), int(side)))
    return out


@njit(cache=True)
def _bases(h, l, c, atr, max_range, push, min_m, max_m, lag):
    """База: от min_m до max_m баров с размахом ≤ max_range·ATR; импульс — закрытие
    дальше края на push·ATR в пределах lag баров. -> (верх, низ, бар импульса, сторона)."""
    n = len(c)
    out = []
    last_up = -1
    last_dn = -1
    for e in range(max_m, n - 1):
        a = atr[e]
        if not np.isfinite(a) or a <= 0:
            continue
        best = -1
        hi = -1e300
        lo = 1e300
        for m in range(1, max_m + 1):
            j = e - m + 1
            hi = max(hi, h[j])
            lo = min(lo, l[j])
            if hi - lo > max_range * a:
                break
            if m >= min_m:
                best = m
        if best < 0:
            continue
        hi = -1e300
        lo = 1e300
        for j in range(e - best + 1, e + 1):
            hi = max(hi, h[j])
            lo = min(lo, l[j])
        for i in range(e + 1, min(e + 1 + lag, n)):
            if c[i] >= hi + push * a:
                if i > last_up:              # одна зона на импульс
                    out.append((hi, lo, i, 1))
                    last_up = i
                break
            if c[i] <= lo - push * a:
                if i > last_dn:
                    out.append((hi, lo, i, -1))
                    last_dn = i
                break
    return out


def zones_z2(B: Bars, atr):
    raw = _bases(B.h, B.l, B.c, atr, 1.0, 2.0, 2, 6, 3)
    n = len(B)
    return [(hi, lo, int(i), min(int(i) + W, n - 1), int(d)) for hi, lo, i, d in raw]


@njit(cache=True)
def _scan_conf(h, l, c, top, bot, known, expire, side):
    """Заход в зону и закрытие обратно за ближнюю границу. side 0 — по положению цены.
    -> (зона, бар события, сторона, экстремум захода, бар захода)."""
    out = []
    for z in range(len(top)):
        for d in (1, -1):
            if side[z] != 0 and side[z] != d:
                continue
            prox = top[z] if d == 1 else bot[z]
            dist = bot[z] if d == 1 else top[z]
            i = known[z] + 1
            done = False
            while i <= expire[z] and not done:
                outside_before = (c[i - 1] - prox) * d > 0
                touched = (l[i] <= prox) if d == 1 else (h[i] >= prox)
                if not (outside_before and touched):
                    if (c[i] - dist) * d < 0:
                        done = True             # закрылись за дальней границей без подхода — зона сломана
                    i += 1
                    continue
                s = i
                ext = l[s] if d == 1 else h[s]
                for m in range(s, min(s + RET + 1, expire[z] + 1)):
                    v = l[m] if d == 1 else h[m]
                    if (v - ext) * d < 0:
                        ext = v
                    if (c[m] - dist) * d < 0:
                        done = True             # пробита насквозь
                        break
                    if (c[m] - prox) * d > 0:
                        out.append((z, m, d, ext, s))
                        done = True
                        break
                i = s + RET + 1
    return out


def build(bars: dict, tf: str, kind: str, src: str, k: int, w: float):
    """События conf и заготовки edge для зоны вида kind ('z1' или 'z2')."""
    B = bars[tf]
    atr = S.atr(B.h, B.l, B.c, 14)
    zs = zones_z1(B, src, k, w, atr) if kind == 'z1' else zones_z2(B, atr)
    if not zs:
        return pd.DataFrame(), pd.DataFrame()
    arr = np.array(zs, dtype=np.float64)
    top, bot = arr[:, 0], arr[:, 1]
    known, expire, side = (arr[:, i].astype(np.int64) for i in (2, 3, 4))
    ev = _scan_conf(B.h, B.l, B.c, atr, top, bot, known, expire, side) if False else \
        _scan_conf(B.h, B.l, B.c, top, bot, known, expire, side)
    conf = pd.DataFrame()
    if ev:
        e = np.array(ev, dtype=np.float64)
        z, i, d = e[:, 0].astype(np.int64), e[:, 1].astype(np.int64), e[:, 2].astype(np.int64)
        far = np.where(d == 1, bot[z], top[z])
        ext = np.where(d == 1, np.minimum(e[:, 3], far), np.maximum(e[:, 3], far))
        conf = pd.DataFrame({'dir': d, 'i': i, 't': B.tclose[i], 'i_place': B.i1[i], 'close': B.c[i],
                             'atr': atr[i], 'stop': ext - d * 0.1 * atr[i], 'entry': np.nan,
                             'width_atr': (top[z] - bot[z]) / atr[i]})
    # edge: лимит на ближней границе с момента, когда зона известна; цена — снаружи
    d_e = np.where(side != 0, side, np.where(B.c[known] > top, 1, np.where(B.c[known] < bot, -1, 0)))
    ok = (d_e != 0) & (((B.c[known] - np.where(d_e == 1, top, bot)) * d_e) > 0) & np.isfinite(atr[known])
    kk = known[ok]
    de = d_e[ok]
    prox = np.where(de == 1, top[ok], bot[ok])
    far = np.where(de == 1, bot[ok], top[ok])
    edge = pd.DataFrame({'dir': de, 'i': kk, 't': B.tclose[kk], 'i_place': B.i1[kk], 'close': prox,
                         'atr': atr[kk], 'stop': far - de * 0.1 * atr[kk], 'entry': prox,
                         'expire_bar': expire[ok], 'width_atr': (top[ok] - bot[ok]) / atr[kk]})
    for df in (conf, edge):
        if len(df):
            df.sort_values('i', kind='stable', inplace=True)
            df.reset_index(drop=True, inplace=True)
    return conf, edge


def orders(df: pd.DataFrame, B: Bars, unit: int, mode: str, target, hold_bars: int) -> dict:
    d = df.dir.to_numpy()
    ref = df.close.to_numpy()
    stop = df.stop.to_numpy()
    risk = (ref - stop) * d
    if target == 'none':
        tg = np.where(d == 1, np.inf, -np.inf)
    else:
        tg = ref + d * float(target) * risk
    per = B.tf // unit
    i_place = df.i_place.to_numpy()
    if mode == 'conf':
        ent = np.full(len(df), np.nan)
        i_expire = i_place + 1
    else:
        ent = df.entry.to_numpy()
        i_expire = B.i1[np.minimum(df.expire_bar.to_numpy(), len(B) - 1)]
    hold = np.minimum(hold_bars * per, 30 * 1440 // unit)
    valid = (risk > 0) & (risk / ref >= E.MIN_STOP)
    return dict(side=d, i_place=i_place, i_expire=i_expire, entry=ent, stop=stop, target=tg,
                hold=np.full(len(df), hold, np.int64), ev=np.arange(len(df)), ref=ref, risk=risk,
                valid=valid)
