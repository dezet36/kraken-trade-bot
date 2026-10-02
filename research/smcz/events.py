"""Таблица событий SMC с признаками — общий материал для всех моделей.

Событие — слом структуры на рабочем ТФ (BOS или CHoCH). К нему крепятся:
нога (начало импульса, экстремум), ордер-блок, имбаланс, и признаки
контекста, известные на закрытии бара слома:
- тренд старших ТФ (1ч, 4ч, 1д) относительно направления слома;
- снятие ликвидности началом ноги: свинг своего ТФ, свинг 1ч/4ч, минимум /
  максимум прошлого дня — и глубина выноса в ATR;
- сила импульса, объём, время суток, положение в диапазоне 4ч
  (дисконт/премия).
Модели A (продолжение от блока), B (снятие + CHoCH), D (ликвидность по
времени) — это срезы этой таблицы; модель C — по признаку «начало ноги в зоне
блока старшего ТФ».
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numba import njit

from smcz import structure as S
from smcz.data import Bars


@njit(cache=True)
def _first_cross(l_ltf, h_ltf, start_idx, px, typ):
    """Для каждого уровня — первый бар младшего ТФ с индекса start_idx, где
    цена прошла за уровень (минимум ниже для typ=−1, максимум выше для +1)."""
    n = len(px)
    out = np.full(n, -1, np.int64)
    N = len(l_ltf)
    for q in range(n):
        s = start_idx[q]
        if s < 0:
            continue
        p = px[q]
        if typ[q] == -1:
            for i in range(s, N):
                if l_ltf[i] < p:
                    out[q] = i
                    break
        else:
            for i in range(s, N):
                if h_ltf[i] > p:
                    out[q] = i
                    break
    return out


@njit(cache=True)
def _sweep_feature(ev_dir, ev_piv, ev_org, ev_orgpx, lv_px, lv_typ, lv_known, lv_cross):
    """Сколько уровней сняло начало ноги и наибольшая глубина выноса (в цене).

    Уровень засчитан, если он известен до бара начала ноги (lv_known ≤ org),
    впервые пройден на отрезке [piv, org] и начало ноги — за ним.
    Для бычьего события — уровни-минимумы, для медвежьего — максимумы."""
    ne = len(ev_dir)
    cnt = np.zeros(ne, np.int64)
    depth = np.zeros(ne)
    nearest = np.full(ne, np.nan)
    # уровни отсортированы по lv_cross; для каждого события ищем окно
    for e in range(ne):
        d = ev_dir[e]
        a = ev_piv[e]
        b = ev_org[e]
        want = -1 if d == 1 else 1
        for q in range(len(lv_px)):
            x = lv_cross[q]
            if x < a or x > b:
                continue
            if lv_typ[q] != want or lv_known[q] > b:
                continue
            if d == 1:
                dep = lv_px[q] - ev_orgpx[e]
            else:
                dep = ev_orgpx[e] - lv_px[q]
            if dep < 0:
                continue
            cnt[e] += 1
            if dep > depth[e]:
                depth[e] = dep
            if np.isnan(nearest[e]) or dep < abs(nearest[e]):
                nearest[e] = dep
    return cnt, depth, nearest


def _sweeps(ev, B: Bars, H: Bars, k_liq: int):
    """Признак снятия ликвидности ТФ H началом ноги события на ТФ B."""
    idx, px, typ, conf, _ = S.liquidity(H.h, H.l, k_liq)
    # момент, когда уровень стал известен, — закрытие бара подтверждения;
    # на рабочем ТФ — первый бар, начавшийся не раньше этого
    known_min = H.tclose[conf]
    lv_known = np.searchsorted(B.t, known_min, side='left')
    lv_known = np.where(lv_known < len(B.t), lv_known, -1)
    cross = _first_cross(B.l, B.h, lv_known, px, typ)
    ok = cross >= 0
    order = np.argsort(cross[ok])
    pxs, typs, kn, cr = px[ok][order], typ[ok][order], lv_known[ok][order], cross[ok][order]
    ev_dir = ev[:, S.EV_DIR].astype(np.int64)
    ev_piv = ev[:, S.EV_PIV].astype(np.int64)
    ev_org = ev[:, S.EV_ORG].astype(np.int64)
    # окно поиска по отсортированным cross: ограничим перебор срезом
    cnt = np.zeros(len(ev), np.int64)
    depth = np.zeros(len(ev))
    for lo_i in range(0, len(ev), 2000):
        hi_i = min(lo_i + 2000, len(ev))
        a = ev_piv[lo_i:hi_i].min()
        b = ev_org[lo_i:hi_i].max()
        s0 = np.searchsorted(cr, a, side='left')
        s1 = np.searchsorted(cr, b, side='right')
        c_, d_, _ = _sweep_feature(ev_dir[lo_i:hi_i], ev_piv[lo_i:hi_i], ev_org[lo_i:hi_i],
                                   ev[lo_i:hi_i, S.EV_ORGPX], pxs[s0:s1], typs[s0:s1],
                                   kn[s0:s1], cr[s0:s1])
        cnt[lo_i:hi_i] = c_
        depth[lo_i:hi_i] = d_
    return cnt, depth


@njit(cache=True)
def _nearest_liq(ev_bar, ev_dir, ev_close, lv_px, lv_typ, lv_conf, lv_swept, lookback):
    """Ближайшая нетронутая ликвидность по ходу сделки на закрытии бара
    события: для покупки — ближайший свинг-максимум выше цены, ещё не снятый.
    Уровни идут по возрастанию подтверждения; смотрим lookback последних."""
    ne = len(ev_bar)
    out = np.full(ne, np.nan)
    L = len(lv_px)
    j = 0
    for e in range(ne):
        i = ev_bar[e]
        while j < L and lv_conf[j] <= i:
            j += 1
        want = 1 if ev_dir[e] == 1 else -1
        best = np.nan
        for q in range(j - 1, max(j - 1 - lookback, -1), -1):
            if lv_typ[q] != want:
                continue
            sw = lv_swept[q]
            if sw != -1 and sw <= i:
                continue
            p = lv_px[q]
            if want == 1:
                if p > ev_close[e] and (np.isnan(best) or p < best):
                    best = p
            else:
                if p < ev_close[e] and (np.isnan(best) or p > best):
                    best = p
        out[e] = best
    return out


def _htf_trend(B: Bars, H: Bars, k: int):
    tr, _ = S.market_structure(H.o, H.h, H.l, H.c, k)
    j = S.align(H.tclose, B.tclose)
    return np.where(j >= 0, tr[np.clip(j, 0, None)], 0)


def _htf_range(B: Bars, H: Bars, k: int):
    """Последние подтверждённые свинг-максимум и минимум ТФ H на закрытии
    каждого бара B — диапазон для дисконта/премии."""
    ph, pl = S.pivots(H.h, H.l, k)
    hi = np.full(len(H), np.nan)
    lo = np.full(len(H), np.nan)
    # свинг в j известен после закрытия бара j+k
    jh = np.flatnonzero(ph)
    jl = np.flatnonzero(pl)
    hi[np.minimum(jh + k, len(H) - 1)] = H.h[jh]
    lo[np.minimum(jl + k, len(H) - 1)] = H.l[jl]
    hi = pd.Series(hi).ffill().to_numpy()
    lo = pd.Series(lo).ffill().to_numpy()
    j = S.align(H.tclose, B.tclose)
    jj = np.clip(j, 0, None)
    return np.where(j >= 0, hi[jj], np.nan), np.where(j >= 0, lo[jj], np.nan)


def _prev_day(B: Bars, D1: Bars):
    """Максимум/минимум прошлого дня UTC на каждом баре B."""
    day = B.t // 1440
    dmap = pd.Series(np.arange(len(D1)), index=D1.t // 1440)
    prev = dmap.reindex(day - 1).to_numpy()
    ok = ~np.isnan(prev)
    pi = np.where(ok, prev, 0).astype(np.int64)
    pdh = np.where(ok, D1.h[pi], np.nan)
    pdl = np.where(ok, D1.l[pi], np.nan)
    return pdh, pdl


def build(bars: dict, tf: str, k: int) -> pd.DataFrame:
    B = bars[tf]
    _, ev = S.market_structure(B.o, B.h, B.l, B.c, k)
    a = S.atr(B.h, B.l, B.c, 14)
    i = ev[:, S.EV_BAR].astype(np.int64)
    d = ev[:, S.EV_DIR].astype(np.int64)
    org = ev[:, S.EV_ORG].astype(np.int64)
    df = pd.DataFrame({
        'bar': i, 'dir': d, 'kind': ev[:, S.EV_KIND].astype(np.int64),
        't': B.tclose[i],                         # минута, когда событие известно
        'i_place': B.i1[i],                       # минутка, с которой ставится заявка
        'close': B.c[i], 'atr': a[i],
        'level': ev[:, S.EV_LEVEL], 'piv': ev[:, S.EV_PIV].astype(np.int64),
        'org': org, 'orgpx': ev[:, S.EV_ORGPX], 'ext': ev[:, S.EV_EXT],
        'obhi': ev[:, S.EV_OBHI], 'oblo': ev[:, S.EV_OBLO], 'obbody': ev[:, S.EV_OBBODY],
        'fvghi': ev[:, S.EV_FVGHI], 'fvglo': ev[:, S.EV_FVGLO],
        'prevopp': ev[:, S.EV_PREVOPP],
    })
    df['leg_bars'] = i - org
    df['disp'] = (df.ext - df.orgpx).abs() / df.atr
    df['brk_body'] = np.abs(B.c[i] - B.o[i]) / a[i]
    qv = pd.Series(B.qv)
    vma = qv.rolling(50, min_periods=10).mean().shift(1).to_numpy()
    df['vol_brk'] = B.qv[i] / vma[i]
    # объём ноги относительно обычного
    cq = np.r_[0.0, np.cumsum(B.qv)]
    leg_q = (cq[i + 1] - cq[org]) / np.maximum(i - org + 1, 1)
    df['vol_leg'] = leg_q / vma[i]
    if not np.all(np.isnan(B.tbq)):
        ct = np.r_[0.0, np.cumsum(B.tbq)]
        df['taker_leg'] = (ct[i + 1] - ct[org]) / np.maximum(cq[i + 1] - cq[org], 1e-9)
    else:
        df['taker_leg'] = np.nan
    df['hour'] = (B.tclose[i] % 1440) // 60
    df['dow'] = ((B.tclose[i] // 1440) + 3) % 7          # 0 — понедельник
    df['atr_pct'] = df.atr / df.close
    for name, kk in (('1h', 3), ('4h', 3), ('12h', 3), ('1d', 2), ('1w', 2)):
        if bars[name].tf > B.tf:
            df[f'tr_{name}'] = _htf_trend(B, bars[name], kk)[i] * d
        else:
            df[f'tr_{name}'] = 0
    hi4, lo4 = _htf_range(B, bars['4h'], 3)
    rng = hi4[i] - lo4[i]
    df['pd4h'] = np.where(rng > 0, (B.c[i] - lo4[i]) / rng, np.nan)   # 0 — низ, 1 — верх
    hiD, loD = _htf_range(B, bars['1d'], 2)
    rng = hiD[i] - loD[i]
    df['pd1d'] = np.where(rng > 0, (B.c[i] - loD[i]) / rng, np.nan)
    # ближайшая ликвидность по ходу (свинги своего ТФ и вдвое крупнее)
    for name, kk in (('liq', k), ('liq2', 2 * k)):
        lidx, lpx, ltyp, lconf, lsw = S.liquidity(B.h, B.l, kk)
        o_ = np.argsort(lconf, kind='stable')
        df[name] = _nearest_liq(i, d, B.c[i], lpx[o_], ltyp[o_], lconf[o_], lsw[o_], 400)
    # встречный слом: первая минутка после закрытия бара ближайшего события
    # в другую сторону (там структура сменилась — идея сделки сломана)
    nxt = np.full(len(df), len(B.i1) - 1, np.int64)
    nb = len(B.i1) - 1
    nu = len(B.i1) - 1
    for e in range(len(df) - 1, -1, -1):
        nxt[e] = nu if d[e] == -1 else nb
        if d[e] == 1:
            nu = i[e]
        else:
            nb = i[e]
    df['opp_bar'] = nxt
    df['i_opp'] = B.i1[nxt]
    D1 = bars['1d']
    dq = pd.Series(D1.qv, index=D1.t // 1440)
    adv = dq.rolling(30, min_periods=20).mean()
    ev_day = B.tclose[i] // 1440
    # оборот прошлых полных дней: значение на день ev_day−1
    df['adv30'] = adv.reindex(ev_day - 1).to_numpy()
    df['age_d'] = (B.tclose[i] - bars['base'].t[0]) / 1440.0
    # снятие ликвидности началом ноги
    for name, src, kk in (('own', tf, k), ('1h', '1h', 3), ('4h', '4h', 3)):
        H = bars[src]
        if H.tf < B.tf:
            df[f'sw_{name}'] = 0
            df[f'swd_{name}'] = 0.0
            continue
        cnt, dep = _sweeps(ev, B, H, kk)
        df[f'sw_{name}'] = cnt
        df[f'swd_{name}'] = dep / a[i]
    pdh, pdl = _prev_day(B, bars['1d'])
    # минимум/максимум прошлого дня снят началом ноги, и до отрезка ноги
    # в этот день цена за него не ходила
    day0 = (B.t // 1440) * 1440
    lv = np.where(d == 1, pdl[org], pdh[org])
    beyond = np.where(d == 1, df.orgpx < lv, df.orgpx > lv)
    # проверка «до ноги не ходили»: экстремум дня до бара piv
    first_in_day = np.searchsorted(B.t, day0[org], side='left')
    lows_before = np.full(len(df), np.nan)
    highs_before = np.full(len(df), np.nan)
    piv = df.piv.to_numpy()
    for e in range(len(df)):
        s, f = first_in_day[e], piv[e]
        if f > s:
            lows_before[e] = B.l[s:f].min()
            highs_before[e] = B.h[s:f].max()
    clean = np.where(d == 1, ~(lows_before < lv), ~(highs_before > lv))
    df['sw_pd'] = (beyond & clean & ~np.isnan(lv)).astype(np.int64)
    df['swd_pd'] = np.where(df.sw_pd == 1, np.abs(df.orgpx - lv) / df.atr, 0.0)
    return df


# ── геометрия заявки ──────────────────────────────────────────────────────────

def orders(df: pd.DataFrame, B: Bars, zone: str, target, expiry_bars: int,
           hold_bars: int, buf_atr: float = 0.1, exit_choch: bool = False,
           unit: int = 1) -> dict:
    """Вход, стоп, цель по событиям.

    zone: 'obw' — край блока по тени, 'obb' — по телу, 'obm' — середина
    блока, 'fvg' — край имбаланса, 'fvgm' — середина имбаланса, 'mkt' — по
    рынку на открытии следующего бара (вход на сломе).
    Стоп — за началом ноги (блоком) на buf_atr·ATR: там ломается идея.
    'lvl' — ретест сломанного уровня.
    target: число — кратное R; 'ext' — экстремум ноги; 'liq'/'liq2' —
    ближайший нетронутый свинг по ходу; 'none' — без цели.
    exit_choch: позиция закрывается на закрытии бара встречного слома
    (заявка, не налитая до него, снимается).
    unit — минут в баре ряда исполнения (1 — минутки, 60 — часы).
    """
    d = df.dir.to_numpy()
    bull = d == 1
    obhi, oblo, obb = df.obhi.to_numpy(), df.oblo.to_numpy(), df.obbody.to_numpy()
    fh, fl = df.fvghi.to_numpy(), df.fvglo.to_numpy()
    if zone == 'obw':
        entry = np.where(bull, obhi, oblo)
    elif zone == 'obb':
        entry = obb
    elif zone == 'obm':
        entry = (obhi + oblo) / 2
    elif zone == 'fvg':
        entry = np.where(bull, fh, fl)
    elif zone == 'fvgm':
        entry = (fh + fl) / 2
    elif zone == 'mkt':
        entry = np.full(len(df), np.nan)
    elif zone == 'lvl':
        entry = df.level.to_numpy()
    else:
        raise ValueError(zone)
    buf = buf_atr * df.atr.to_numpy()
    stop = np.where(bull, oblo - buf, obhi + buf)
    ref = np.where(np.isnan(entry), df.close.to_numpy(), entry)
    risk = (ref - stop) * d
    if target == 'ext':
        tgt = df.ext.to_numpy()
    elif target in ('liq', 'liq2'):
        tgt = df[target].to_numpy()
    elif target == 'none':
        tgt = np.where(bull, np.inf, -np.inf)
    else:
        tgt = ref + d * float(target) * risk
    i_place = df.i_place.to_numpy()
    per = B.tf // unit                      # баров исполнения в баре рабочего ТФ
    i_expire = i_place + expiry_bars * per
    out = dict(side=d, i_place=i_place, entry=entry, stop=stop, target=tgt,
               hold=np.full(len(df), hold_bars * per), ev=np.arange(len(df)),
               ref=ref, risk=risk)
    if exit_choch:
        i_opp = df.i_opp.to_numpy()
        i_expire = np.minimum(i_expire, i_opp)
        out['i_deadline'] = i_opp - 1
    out['i_expire'] = i_expire
    return out
