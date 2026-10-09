"""
SMC-структура 4ч: свинги, слом структуры, нога слома, сетап — чистая логика.

Построчный перенос research/smcz/structure.py (там — numba, здесь — чистый
Python: на 500 барах это доли секунды, а зависимость на сервере не нужна).
Совпадение с исследовательским движком проверяет tests/test_smcs_core.py на
записанных из него событиях: торговаться должно ровно то, что измерено.

Всё «вживую»: свинг в j известен после закрытия бара j+k, слом на баре i —
после закрытия бара i. Заглядывания вперёд нет.
"""

import math


def atr(h, l, c, n=14):
    """ATR Уайлдера; первые n баров — среднее истинных диапазонов."""
    out = [0.0] * len(h)
    if not h:
        return out
    a = h[0] - l[0]
    out[0] = a
    for i in range(1, len(h)):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        if i < n:
            a = (a * i + tr) / (i + 1)
        else:
            a = (a * (n - 1) + tr) / n
        out[i] = a
    return out


def pivots(h, l, k):
    """Свинг-максимум в j: строго выше k баров слева, не ниже k баров справа."""
    n = len(h)
    ph = [False] * n
    pl = [False] * n
    for j in range(k, n - k):
        hj = h[j]
        ok = all(h[m] < hj for m in range(j - k, j)) and \
            all(h[m] <= hj for m in range(j + 1, j + k + 1))
        ph[j] = ok
        lj = l[j]
        ok = all(l[m] > lj for m in range(j - k, j)) and \
            all(l[m] >= lj for m in range(j + 1, j + k + 1))
        pl[j] = ok
    return ph, pl


def market_structure(o, h, l, c, k):
    """
    Сломы структуры по закрытию за последним подтверждённым свингом.

    Возвращает (trend, events). trend[i] — тренд после закрытия бара i
    (+1 / −1 / 0). Событие — словарь: bar, dir (+1/−1), kind ('BOS' — по ходу
    текущей структуры, 'CHoCH' — против), level (сломанный свинг), piv
    (его бар), org (бар начала ноги), org_px, ext (экстремум ноги), ob,
    ob_hi, ob_lo (ордер-блок ноги).
    """
    n = len(h)
    ph, pl = pivots(h, l, k)
    trend = [0] * n
    events = []
    t = 0
    hp = hpi = None
    hb = True
    lp = lpi = None
    lb = True
    for i in range(n):
        j = i - k
        if j >= 0:
            if ph[j]:
                hp, hpi, hb = h[j], j, False
            if pl[j]:
                lp, lpi, lb = l[j], j, False
        bull = (not hb) and c[i] > hp
        bear = (not lb) and c[i] < lp
        if bull and bear:
            bull = bear = False
        if bull:
            org = hpi
            for m in range(hpi, i + 1):
                if l[m] < l[org]:
                    org = m
            ext = max(h[org:i + 1])
            ob = org
            if c[org] >= o[org]:
                for m in range(org - 1, max(org - 4, -1), -1):
                    if c[m] < o[m]:
                        ob = m
                        break
            events.append({'bar': i, 'dir': 1, 'kind': 'BOS' if t == 1 else 'CHoCH',
                           'level': hp, 'piv': hpi, 'org': org, 'org_px': l[org],
                           'ext': ext, 'ob': ob, 'ob_hi': h[ob],
                           'ob_lo': min(l[ob], l[org])})
            t = 1
            hb = True
        elif bear:
            org = lpi
            for m in range(lpi, i + 1):
                if h[m] > h[org]:
                    org = m
            ext = min(l[org:i + 1])
            ob = org
            if c[org] <= o[org]:
                for m in range(org - 1, max(org - 4, -1), -1):
                    if c[m] > o[m]:
                        ob = m
                        break
            events.append({'bar': i, 'dir': -1, 'kind': 'BOS' if t == -1 else 'CHoCH',
                           'level': lp, 'piv': lpi, 'org': org, 'org_px': h[org],
                           'ext': ext, 'ob': ob, 'ob_hi': max(h[ob], h[org]),
                           'ob_lo': l[ob]})
            t = -1
            lb = True
        trend[i] = t
    return trend, events


def evaluate(o, h, l, c, k=3, atr_period=14, stop_buffer_atr=0.1,
             target_r=8.0, bos_only=True):
    """
    Сетап на ПОСЛЕДНЕМ баре (он должен быть закрытым) или (None, причина).

    Сетап: направление, вход-ориентир (закрытие бара слома), стоп за
    началом ноги + буфер ATR, цель = ориентир ± target_r·R. Так считал замер:
    R — от закрытия бара слома, не от цены заполнения.
    """
    n = len(c)
    if n < 4 * k + atr_period:
        return None, 'мало свечей'
    _, events = market_structure(o, h, l, c, k)
    if not events or events[-1]['bar'] != n - 1:
        return None, 'на последнем баре слома нет'
    ev = events[-1]
    if bos_only and ev['kind'] != 'BOS':
        return None, 'слом против структуры (CHoCH) — не берётся'
    a = atr(h, l, c, atr_period)[n - 1]
    ref = c[n - 1]
    d = ev['dir']
    stop = ev['ob_lo'] - stop_buffer_atr * a if d == 1 else ev['ob_hi'] + stop_buffer_atr * a
    risk = (ref - stop) * d
    if not (risk > 0) or not math.isfinite(risk):
        return None, 'стоп не по ту сторону цены'
    target = ref + d * target_r * risk
    return {
        'direction': 'LONG' if d == 1 else 'SHORT',
        'dir': d,
        'kind': ev['kind'],
        'ref': ref,
        'stop': stop,
        'target': target,
        'risk': risk,
        'stop_pct': risk / ref * 100,
        'atr': a,
        'level': ev['level'],
        'org_px': ev['org_px'],
        'ext': ev['ext'],
        'ob_hi': ev['ob_hi'],
        'ob_lo': ev['ob_lo'],
        'leg_bars': n - 1 - ev['org'],
        'disp_atr': abs(ev['ext'] - ev['org_px']) / a if a else 0.0,
        'org_back': n - 1 - ev['org'],
        'piv_back': n - 1 - ev['piv'],
    }, 'BOS'
