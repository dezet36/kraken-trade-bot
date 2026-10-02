"""Структура рынка по Smart Money — с нуля.

Всё считается «вживую»: элемент, найденный на баре i, известен только после
закрытия бара i (свинг — после закрытия бара j+k). Заглядывания вперёд нет:
это проверяет tests в конце файла (python -m smcz.structure).

Элементы:
- свинги (фракталы с k барами по обе стороны);
- слом структуры: закрытие за последним подтверждённым свингом. Если слом в
  сторону текущего тренда — BOS (продолжение), против — CHoCH (смена);
- нога слома: от экстремума между сломанным свингом и баром слома (начало
  импульса, origin) до бара слома;
- ордер-блок ноги: последняя противоположная свеча у начала импульса;
- имбаланс (FVG) ноги: первый разрыв трёх свечей после начала импульса;
- ликвидность: подтверждённые свинги и момент, когда их сняли.
"""
from __future__ import annotations

import numpy as np
from numba import njit


@njit(cache=True)
def atr(h, l, c, n=14):
    out = np.empty(len(h))
    tr0 = h[0] - l[0]
    a = tr0
    out[0] = a
    for i in range(1, len(h)):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        if i < n:
            a = (a * i + tr) / (i + 1)
        else:
            a = (a * (n - 1) + tr) / n
        out[i] = a
    return out


@njit(cache=True)
def pivots(h, l, k):
    """Свинг-максимум в j: строго выше k баров слева, не ниже k баров справа."""
    n = len(h)
    ph = np.zeros(n, np.bool_)
    pl = np.zeros(n, np.bool_)
    for j in range(k, n - k):
        hj = h[j]
        ok = True
        for m in range(j - k, j):
            if h[m] >= hj:
                ok = False
                break
        if ok:
            for m in range(j + 1, j + k + 1):
                if h[m] > hj:
                    ok = False
                    break
        ph[j] = ok
        lj = l[j]
        ok = True
        for m in range(j - k, j):
            if l[m] <= lj:
                ok = False
                break
        if ok:
            for m in range(j + 1, j + k + 1):
                if l[m] < lj:
                    ok = False
                    break
        pl[j] = ok
    return ph, pl


@njit(cache=True)
def liquidity(h, l, k):
    """Свинги как пулы ликвидности.

    Возвращает массивы по каждому свингу: индекс, цена, тип (+1 максимум,
    −1 минимум), индекс подтверждения (j+k), индекс первого бара, который
    прошёл за уровень после подтверждения (снятие), −1 — ещё не снят.
    """
    ph, pl = pivots(h, l, k)
    n = len(h)
    cnt = 0
    for j in range(n):
        if ph[j]:
            cnt += 1
        if pl[j]:
            cnt += 1
    idx = np.empty(cnt, np.int64)
    px = np.empty(cnt)
    typ = np.empty(cnt, np.int64)
    conf = np.empty(cnt, np.int64)
    swept = np.full(cnt, -1, np.int64)
    m = 0
    for j in range(n):
        if ph[j]:
            idx[m] = j
            px[m] = h[j]
            typ[m] = 1
            conf[m] = j + k
            for i in range(j + k + 1, n):
                if h[i] > h[j]:
                    swept[m] = i
                    break
            m += 1
        if pl[j]:
            idx[m] = j
            px[m] = l[j]
            typ[m] = -1
            conf[m] = j + k
            for i in range(j + k + 1, n):
                if l[i] < l[j]:
                    swept[m] = i
                    break
            m += 1
    return idx, px, typ, conf, swept


# поля события слома
EV_BAR, EV_DIR, EV_KIND, EV_LEVEL, EV_PIV, EV_ORG, EV_ORGPX, EV_EXT, \
    EV_OB, EV_OBHI, EV_OBLO, EV_OBBODY, EV_FVG, EV_FVGHI, EV_FVGLO, \
    EV_PREVOPP = range(16)
EV_N = 16


@njit(cache=True)
def market_structure(o, h, l, c, k):
    """Слом структуры по закрытию за последним подтверждённым свингом.

    trend[i] — тренд после закрытия бара i (+1 / −1 / 0 — ещё не определён).
    events — строки по полям EV_*; цены для бычьего слома:
      ORGPX — минимум ноги (начало импульса), EXT — максимум ноги на баре слома,
      OBHI/OBLO — тени ордер-блока, OBBODY — верх тела блока,
      FVGHI/FVGLO — границы первого имбаланса ноги (nan — нет),
      PREVOPP — цена последнего свинга против слома, подтверждённого до него
      (для бычьего — свинг-минимум; это «защищённый» уровень).
    Для медвежьего — зеркально (OBBODY — низ тела блока).
    """
    n = len(h)
    ph, pl = pivots(h, l, k)
    trend = np.zeros(n, np.int64)
    ev = np.full((n // 2 + 1, EV_N), np.nan)
    ne = 0
    t = 0
    hp = np.nan
    hpi = -1
    hb = True
    lp = np.nan
    lpi = -1
    lb = True
    for i in range(n):
        j = i - k
        if j >= 0:
            if ph[j]:
                hp = h[j]
                hpi = j
                hb = False
            if pl[j]:
                lp = l[j]
                lpi = j
                lb = False
        bull = (not hb) and c[i] > hp
        bear = (not lb) and c[i] < lp
        if bull and bear:
            bull = False
            bear = False
        if bull:
            org = hpi
            for m in range(hpi, i + 1):
                if l[m] < l[org]:
                    org = m
            ext = h[org]
            for m in range(org, i + 1):
                if h[m] > ext:
                    ext = h[m]
            ob = org
            if c[org] >= o[org]:
                for m in range(org - 1, max(org - 4, -1), -1):
                    if c[m] < o[m]:
                        ob = m
                        break
            fv = -1
            for m in range(org + 2, i + 1):
                if l[m] > h[m - 2]:
                    fv = m
                    break
            ev[ne, EV_BAR] = i
            ev[ne, EV_DIR] = 1
            ev[ne, EV_KIND] = 0 if t == 1 else 1
            ev[ne, EV_LEVEL] = hp
            ev[ne, EV_PIV] = hpi
            ev[ne, EV_ORG] = org
            ev[ne, EV_ORGPX] = l[org]
            ev[ne, EV_EXT] = ext
            ev[ne, EV_OB] = ob
            ev[ne, EV_OBHI] = h[ob]
            ev[ne, EV_OBLO] = min(l[ob], l[org])
            ev[ne, EV_OBBODY] = max(o[ob], c[ob])
            if fv >= 0:
                ev[ne, EV_FVG] = fv
                ev[ne, EV_FVGHI] = l[fv]
                ev[ne, EV_FVGLO] = h[fv - 2]
            ev[ne, EV_PREVOPP] = lp
            ne += 1
            t = 1
            hb = True
        elif bear:
            org = lpi
            for m in range(lpi, i + 1):
                if h[m] > h[org]:
                    org = m
            ext = l[org]
            for m in range(org, i + 1):
                if l[m] < ext:
                    ext = l[m]
            ob = org
            if c[org] <= o[org]:
                for m in range(org - 1, max(org - 4, -1), -1):
                    if c[m] > o[m]:
                        ob = m
                        break
            fv = -1
            for m in range(org + 2, i + 1):
                if h[m] < l[m - 2]:
                    fv = m
                    break
            ev[ne, EV_BAR] = i
            ev[ne, EV_DIR] = -1
            ev[ne, EV_KIND] = 0 if t == -1 else 1
            ev[ne, EV_LEVEL] = lp
            ev[ne, EV_PIV] = lpi
            ev[ne, EV_ORG] = org
            ev[ne, EV_ORGPX] = h[org]
            ev[ne, EV_EXT] = ext
            ev[ne, EV_OB] = ob
            ev[ne, EV_OBHI] = max(h[ob], h[org])
            ev[ne, EV_OBLO] = l[ob]
            ev[ne, EV_OBBODY] = min(o[ob], c[ob])
            if fv >= 0:
                ev[ne, EV_FVG] = fv
                ev[ne, EV_FVGHI] = l[fv - 2]
                ev[ne, EV_FVGLO] = h[fv]
            ev[ne, EV_PREVOPP] = hp
            ne += 1
            t = -1
            lb = True
        trend[i] = t
    return trend, ev[:ne]


def align(t_src_close: np.ndarray, t_dst_close: np.ndarray) -> np.ndarray:
    """Для каждого бара-получателя — индекс последнего бара-источника,
    закрывшегося не позже (−1 — ещё ни одного). Время — минута закрытия."""
    return np.searchsorted(t_src_close, t_dst_close, side='right') - 1


def _selftest():
    """Заглядывания вперёд нет: обрезанный ряд даёт те же события."""
    rng = np.random.default_rng(1)
    n = 3000
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.003, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.003, n)))
    for k in (2, 3, 5):
        tr, ev = market_structure(o, h, l, c, k)
        for cut in (500, 1234, 2999):
            tr2, ev2 = market_structure(o[:cut], h[:cut], l[:cut], c[:cut], k)
            assert np.array_equal(tr[:cut], tr2), (k, cut)
            e1 = ev[ev[:, EV_BAR] < cut]
            assert e1.shape == ev2.shape, (k, cut, e1.shape, ev2.shape)
            assert np.allclose(np.nan_to_num(e1, nan=-7), np.nan_to_num(ev2, nan=-7))
        # бычий слом: закрытие выше уровня, начало ноги ниже закрытия
        b = ev[ev[:, EV_DIR] == 1]
        assert np.all(c[b[:, EV_BAR].astype(int)] > b[:, EV_LEVEL])
        assert np.all(b[:, EV_OBLO] <= b[:, EV_ORGPX] + 1e-12)
    print('structure: ok')


if __name__ == '__main__':
    _selftest()
