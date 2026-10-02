"""Модель C: зона старшего ТФ + подтверждение CHoCH младшего ТФ.

Слом структуры на старшем ТФ (4ч) оставляет ордер-блок ноги. Ждём, пока
цена вернётся в блок; после касания ждём на младшем ТФ (15м/1ч) слом в
сторону старшего (для покупки — бычий слом младшего ТФ). Вход — по рынку на
минуте после закрытия бара младшего слома. Стоп — за блоком старшего ТФ и за
минимумом отката после касания (там ломается идея старшего ТФ). Ожидание
касания и подтверждения ограничено; закрытие младшего ТФ за блоком — зона
сломана, заявки нет.

Возвращает сигналы с теми же полями, что events.orders, и номером события
старшего ТФ — признаки события (BOS/CHoCH, тренд 1д, импульс) берутся из
таблицы событий.
"""
from __future__ import annotations

import numpy as np
from numba import njit

from smcz import structure as S


@njit(cache=True)
def _scan(ev_dir, ev_t, zone_hi, zone_lo, buf, lt_t, lt_tclose, lt_h, lt_l, lt_c,
          lt_bull_brk, lt_bear_brk, wait_min, confirm_min):
    """Для каждого события старшего ТФ — индекс бара младшего ТФ с
    подтверждающим сломом (−1 — нет) и экстремум отката после касания."""
    ne = len(ev_dir)
    sig = np.full(ne, -1, np.int64)
    ext = np.full(ne, np.nan)
    N = len(lt_t)
    j0 = 0
    for e in range(ne):
        t0 = ev_t[e]
        while j0 < N and lt_t[j0] < t0:
            j0 += 1
        d = ev_dir[e]
        hi = zone_hi[e]
        lo = zone_lo[e]
        touched = False
        t_touch = 0
        x = np.nan
        for j in range(j0, N):
            if lt_tclose[j] - t0 > wait_min:
                break
            if d == 1:
                if lt_c[j] < lo - buf[e]:
                    break                       # блок сломан закрытием
                if not touched and lt_l[j] <= hi:
                    touched = True
                    t_touch = lt_t[j]
                if touched:
                    if np.isnan(x) or lt_l[j] < x:
                        x = lt_l[j]
                    if lt_tclose[j] - t_touch > confirm_min:
                        break
                    if lt_bull_brk[j]:
                        sig[e] = j
                        ext[e] = x
                        break
            else:
                if lt_c[j] > hi + buf[e]:
                    break
                if not touched and lt_h[j] >= lo:
                    touched = True
                    t_touch = lt_t[j]
                if touched:
                    if np.isnan(x) or lt_h[j] > x:
                        x = lt_h[j]
                    if lt_tclose[j] - t_touch > confirm_min:
                        break
                    if lt_bear_brk[j]:
                        sig[e] = j
                        ext[e] = x
                        break
    return sig, ext


def signals(ev, bars, htf: str, ltf: str, k_ltf: int, zone='obw', wait_h=120,
            confirm_h=24, buf_atr=0.1):
    """Сигналы модели C по таблице событий старшего ТФ ev."""
    L = bars[ltf]
    _, lev = S.market_structure(L.o, L.h, L.l, L.c, k_ltf)
    bull = np.zeros(len(L), np.bool_)
    bear = np.zeros(len(L), np.bool_)
    b = lev[:, S.EV_BAR].astype(np.int64)
    dd = lev[:, S.EV_DIR]
    bull[b[dd == 1]] = True
    bear[b[dd == -1]] = True
    d = ev.dir.to_numpy()
    if zone == 'obw':
        hi, lo = ev.obhi.to_numpy(), ev.oblo.to_numpy()
    elif zone == 'obb':
        hi = np.where(d == 1, ev.obbody, ev.obhi).astype(float)
        lo = np.where(d == 1, ev.oblo, ev.obbody).astype(float)
    else:
        raise ValueError(zone)
    buf = buf_atr * ev.atr.to_numpy()
    sig, x = _scan(d.astype(np.int64), ev.t.to_numpy().astype(np.int64), hi, lo, buf,
                   L.t, L.tclose, L.h, L.l, L.c, bull, bear, wait_h * 60, confirm_h * 60)
    ok = sig >= 0
    j = sig[ok]
    out = {
        'ev': np.flatnonzero(ok),
        'side': d[ok],
        'i_place': L.i1[j],                    # минутка после закрытия бара младшего слома
        't_sig': L.tclose[j],
        'entry': np.full(ok.sum(), np.nan),    # по рынку
        'ref': L.c[j],
        'stop': np.where(d[ok] == 1, np.minimum(lo[ok], x[ok]) - buf[ok],
                         np.maximum(hi[ok], x[ok]) + buf[ok]),
    }
    out['risk'] = (out['ref'] - out['stop']) * out['side']
    return out
