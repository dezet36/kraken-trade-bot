"""
Импульс «до первой коррекции» — общий слой структуры (28.10.2026).

ЧТО ЭТО. Определение владельца: импульс тянется, пока цена обновляет
экстремум; откаты мельче доли `fix` (0.382) от уже пройденного его не
прерывают. ПЕРВАЯ коррекция к этой доле фиксирует импульс: конец — экстремум
перед ней. Начало — свежий экстремум за `lookback` часов («с нуля») или дно
коррекции прошлого импульса («продолжение»). Дольше `max_track` баров без
коррекции — не импульс, слежение сбрасывается.

Здесь — только описание рынка: где импульсы, какой они длины и силы, где
внутри имбалансы. Что с ними делать (какие брать, где вход, стоп, цели) —
решает стратегия; доля фиксации, глубина поиска начала и предел слежения —
часть определения импульса и приходят от неё же (как правила решений SMC в
analysis/smc). Модуль новый: существующих расчётов не меняет.

Построчный перенос лабораторного движка (D:/WORK_AI_Crypto/_fibo_lab_2026-10-28/
imp_lab.legs_up и quality) на чистый Python; совпадение держит
tests/test_fibv2_wiring.py.
"""


def atr_sma(h, l, c, n=14):
    """Средний истинный диапазон — простое среднее последних n (на первых барах — сколько есть)."""
    out, trs, acc = [], [], 0.0
    for i in range(len(h)):
        tr = (h[i] - l[i]) if i == 0 else (max(h[i], c[i - 1]) - min(l[i], c[i - 1]))
        trs.append(tr)
        acc += tr
        if len(trs) > n:
            acc -= trs[-n - 1]
        out.append(acc / min(len(trs), n))
    return out


def _legs_up(H, L, fix, lookback, max_track):
    """Импульсы вверх по (H, L): [(s, e, k, вид)] — k бар первой коррекции; и состояние
    на конце ряда {'s', 'e', 'kind'} — импульс, который ещё тянется."""
    out = []
    s = e = None
    kind = floor = None
    for k in range(len(H)):
        if s is not None and e is not None:
            size = H[e] - L[s]
            if L[k] <= H[e] - fix * size:              # первая коррекция — импульс зафиксирован
                out.append((s, e, k, kind))
                floor, s, e, kind = L[s], k, None, 'cont'   # следующий — от дна этой коррекции
                continue
        if s is None or L[k] < L[s]:
            low_a = min(L[k - lookback:k + 1]) if k >= lookback else None
            if kind == 'cont' and s is not None and L[k] >= floor:
                s, e = k, None                         # дно коррекции опускается, выше начала прошлого
            elif low_a is not None and L[k] <= low_a:
                s, e, kind, floor = k, None, 'fresh', None
            else:
                s = e = None
            continue
        if k > s and (e is None or H[k] > H[e]):
            e = k
        if k - s > max_track:
            s = e = None
    return out, {'s': s, 'e': e, 'kind': kind}


def legs(h, l, direction, fix=0.382, lookback=24, max_track=72):
    """Импульсы стороны direction (1 — вверх, −1 — вниз) по закрытым барам:
    (список (s, e, k, вид), состояние на конце ряда)."""
    if direction == 1:
        return _legs_up(h, l, fix, lookback, max_track)
    return _legs_up([-x for x in l], [-x for x in h], fix, lookback, max_track)


def ends(h, l, direction, s, e):
    """Цены начала и конца импульса."""
    return (l[s], h[e]) if direction == 1 else (h[s], l[e])


def fixed_now(h, l, direction, state, fix, bar_high, bar_low):
    """Фиксирует ли формирующаяся свеча (её экстремумы на сейчас) импульс, который
    тянулся на закрытых барах: дошла ли она до первой коррекции к fix."""
    s, e = state.get('s'), state.get('e')
    if s is None or e is None:
        return False
    sp, ep = ends(h, l, direction, s, e)
    threshold = ep - direction * fix * abs(ep - sp)
    return bar_low <= threshold if direction == 1 else bar_high >= threshold


def quality(o, h, l, c, atr, direction, s, e):
    """
    Каков импульс: длина, размер, сила к ATR до него, доля свечей по ходу,
    имбалансы внутри. Имбаланс (FVG) на свече m — разрыв между хаем m−1 и лоу
    m+1 (для падения — между лоу m−1 и хаем m+1); глубины краёв — в долях
    импульса от его конца (0 — конец, 1 — начало): (ближний, дальний).
    """
    sp, ep = ends(h, l, direction, s, e)
    size = abs(ep - sp)
    bars = e - s + 1
    a0 = atr[s - 1] if s >= 1 else atr[s]
    rng = [h[m] - l[m] for m in range(s, e + 1)]
    with_move = sum(1 for m in range(s, e + 1) if (c[m] > o[m] if direction == 1 else c[m] < o[m]))
    gaps = []
    for m in range(s + 1, e):
        if direction == 1 and l[m + 1] > h[m - 1]:
            gaps.append(((ep - l[m + 1]) / size, (ep - h[m - 1]) / size))
        if direction == -1 and h[m + 1] < l[m - 1]:
            gaps.append(((h[m + 1] - ep) / size, (l[m - 1] - ep) / size))
    size_atr = size / a0 if a0 > 0 else 0.0
    return {
        'direction': direction, 's': s, 'e': e, 'start': sp, 'end': ep, 'size': size,
        'size_pct': size / ep * 100 if ep else 0.0, 'bars': bars,
        'size_atr': size_atr, 'atr_per_bar': size_atr / bars,
        'dir_share': with_move / bars,
        'efficiency': size / sum(rng) if sum(rng) > 0 else 0.0,
        'gaps': gaps,
    }
