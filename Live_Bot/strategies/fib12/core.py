"""
Фибо 12ч: построчный перенос исследовательского движка (research/fibz/legs.py,
research/smcz/structure.pivots и atr) на чистый Python.

Совпадение с замером держит tests/test_fib12_core.py на реальных свечах
Bybit 12ч (эталон — research/fibz/make_live_fixture.py): строгое «>» против
«>=» у фрактала сдвигает свинг, свинг — ногу, нога — сделку.
"""


def atr(h, l, c, n=14):
    """ATR Уайлдера; первые n баров — среднее истинного диапазона."""
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
    """Свинг-максимум в j: строго выше k баров слева, не ниже k баров справа
    (минимум — зеркально)."""
    n = len(h)
    ph = [False] * n
    pl = [False] * n
    for j in range(k, n - k):
        hj = h[j]
        ph[j] = (all(h[m] < hj for m in range(j - k, j))
                 and all(h[m] <= hj for m in range(j + 1, j + k + 1)))
        lj = l[j]
        pl[j] = (all(l[m] > lj for m in range(j - k, j))
                 and all(l[m] >= lj for m in range(j + 1, j + k + 1)))
    return ph, pl


def legs(h, l, k):
    """
    Ноги в порядке подтверждения. Нога покупки: A — минимум между прошлым
    подтверждённым свинг-максимумом и B, B — свинг-максимум и максимум ноги;
    известна на закрытии бара conf = B + k. Продажа — зеркально.
    """
    ph, pl = pivots(h, l, k)
    out = []
    last_hi = last_lo = -1
    for c in range(len(h)):
        j = c - k
        if j < 0:
            continue
        if ph[j]:
            s = last_hi + 1 if last_hi >= 0 else max(0, j - 200)
            if s < j:
                a = s
                for m in range(s, j):
                    if l[m] < l[a]:
                        a = m
                if all(h[m] <= h[j] for m in range(a, j)) and h[j] > l[a]:
                    out.append({'dir': 1, 'a': a, 'b': j, 'conf': c, 'A': l[a], 'B': h[j]})
            last_hi = j
        if pl[j]:
            s = last_lo + 1 if last_lo >= 0 else max(0, j - 200)
            if s < j:
                a = s
                for m in range(s, j):
                    if h[m] > h[a]:
                        a = m
                if all(l[m] >= l[j] for m in range(a, j)) and h[a] > l[j]:
                    out.append({'dir': -1, 'a': a, 'b': j, 'conf': c, 'A': h[a], 'B': l[j]})
            last_lo = j
    return out


def geometry(leg, close, atr_value, retrace=0.382, stop_level=0.786, stop_buffer_atr=0.1,
             target_ext=1.618):
    """Вход, стоп, цель ноги — как research/fibz/legs.orders (стоп '786',
    цель 'e2618'). None, если к подтверждению цена уже за уровнем входа."""
    d, A, B = leg['dir'], leg['A'], leg['B']
    L = (B - A) * d
    entry = B - d * retrace * L
    stop = B - d * stop_level * L - d * stop_buffer_atr * atr_value
    target = B + d * target_ext * L
    risk = (entry - stop) * d
    if not ((close - entry) * d > 0 and risk > 0 and (target - entry) * d > 0):
        return None
    return {'entry': entry, 'stop': stop, 'target': target, 'risk': risk, 'size': L}


def evaluate(o, h, l, c, k=3, atr_period=14, retrace=0.382, stop_level=0.786,
             stop_buffer_atr=0.1, target_ext=1.618):
    """
    Сетап, рождённый на ПОСЛЕДНЕМ (закрытом) баре, или (None, причина).
    Если на одном баре подтвердились обе ноги — берётся первая, как в замере
    (одна позиция на пару, заявки по порядку).
    """
    n = len(c)
    if n < 4 * k + atr_period:
        return None, 'мало свечей'
    fresh = [g for g in legs(h, l, k) if g['conf'] == n - 1]
    if not fresh:
        return None, 'новой ноги нет'
    a = atr(h, l, c, atr_period)[n - 1]
    for g in fresh:
        geo = geometry(g, c[n - 1], a, retrace, stop_level, stop_buffer_atr, target_ext)
        if geo is None:
            continue
        return ({**g, **geo, 'direction': 'LONG' if g['dir'] == 1 else 'SHORT', 'atr': a,
                 'ref': c[n - 1], 'stop_pct': geo['risk'] / geo['entry'] * 100,
                 'size_atr': geo['size'] / a if a else 0.0, 'leg_bars': g['b'] - g['a']},
                'ok')
    return None, 'к подтверждению откат уже глубже уровня входа'
