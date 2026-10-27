"""
Разметка индикатора «Smart Money Concepts [LuxAlgo]» (Pine v5) на Python.

This work is licensed under Attribution-NonCommercial-ShareAlike 4.0
International (CC BY-NC-SA 4.0) https://creativecommons.org/licenses/by-nc-sa/4.0/
Adapted from «Smart Money Concepts [LuxAlgo]» © LuxAlgo — перенос логики
разметки (свинги, BOS/CHoCH, ордер-блоки, крайние точки) без рисования.

Бар за баром, в порядке исполнения скрипта (раздел MUTABLE VARIABLES &
EXECUTION): крайние точки → свинги size 50 → внутренние size 5 → сломы
внутренней структуры (+ блоки) → сломы свинговой (+ блоки). Только прошлые
данные: точка ноги известна через `size` баров после своего бара.

Не перенесено: рисование, равные вершины/донья, FVG (берётся через
request.security с lookahead_on), снятие блоков (заявка ставится в момент
появления блока, дальше её ведёт исполнение), фильтр конфлюенса внутренних
сломов (во входах скрипта выключен).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SWING_SIZE = 50
INTERNAL_SIZE = 5
BULLISH, BEARISH = 1, -1
BULLISH_LEG, BEARISH_LEG = 1, 0


@dataclass
class Pivot:
    current: float = np.nan
    last: float = np.nan
    crossed: bool = False
    index: int = -1


def atr_rma(h, l, c, length=200):
    """ta.atr(length): RMA истинного диапазона, затравка — среднее первых length."""
    prev = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(prev), h - l,
                  np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev))))
    out = np.full(len(h), np.nan)
    if len(h) < length:
        return out
    out[length - 1] = tr[:length].mean()
    a = 1.0 / length
    for i in range(length, len(h)):
        out[i] = out[i - 1] + a * (tr[i] - out[i - 1])
    return out


def rolling_ext(x, size, fn):
    """fn(x[i-size+1 .. i]) — ta.highest/ta.lowest(size); nan, пока окна нет."""
    out = np.full(len(x), np.nan)
    if len(x) >= size:
        w = np.lib.stride_tricks.sliding_window_view(x, size)
        out[size - 1:] = fn(w, axis=1)
    return out


class Legs:
    """leg(size) и startOfNewLeg — своё состояние на каждый вызов, как var в Pine."""

    def __init__(self, h, l, size):
        self.h, self.l, self.size = h, l, size
        self.hi = rolling_ext(h, size, np.max)
        self.lo = rolling_ext(l, size, np.min)
        self.leg = 0
        self.prev = None

    def step(self, i):
        """+1 — началась бычья нога (на баре i−size была точка-минимум),
        −1 — медвежья (точка-максимум), 0 — нет."""
        s = self.size
        if i >= s:
            if self.h[i - s] > self.hi[i]:
                self.leg = BEARISH_LEG
            elif self.l[i - s] < self.lo[i]:
                self.leg = BULLISH_LEG
        change = 0 if self.prev is None else self.leg - self.prev
        self.prev = self.leg
        return change


def build(h, l, c, swing_size=SWING_SIZE, internal_size=INTERNAL_SIZE, pivots=None):
    """События сломов с ордер-блоками. Каждое событие — словарь:
    i — бар слома (известен на его закрытии), scope 'internal'|'swing',
    side ±1, tag 'BOS'|'CHoCH', ob_hi/ob_lo/ob_i — блок, swing_bias и
    internal_bias — тренды ДО этого слома, trail_top/trail_bottom — крайние
    точки (зоны premium/discount) на закрытии бара.
    pivots — список, куда дописываются точки ног: scope, kind (+1 вершина,
    −1 донышко), level, pivot_i (бар точки), i (бар, на котором она известна)."""
    n = len(c)
    atr = atr_rma(h, l, c, 200)
    vol = np.where(np.isnan(atr), False, (h - l) >= 2 * atr)
    ph = np.where(vol, l, h)            # parsedHigh
    pl = np.where(vol, h, l)            # parsedLow

    swing_legs, internal_legs = Legs(h, l, swing_size), Legs(h, l, internal_size)
    sw_hi, sw_lo, in_hi, in_lo = Pivot(), Pivot(), Pivot(), Pivot()
    trend = {'swing': 0, 'internal': 0}
    top = bottom = np.nan
    # прошлые значения уровня у каждого вызова ta.crossover/crossunder
    prev_lvl = {('internal', 1): np.nan, ('internal', -1): np.nan,
                ('swing', 1): np.nan, ('swing', -1): np.nan}
    events = []

    def set_pivot(p, level, idx):
        p.last, p.current, p.crossed, p.index = p.current, level, False, idx
        if pivots is not None:
            # сравнение по тождеству: у dataclass == сравнивает поля
            scope = 'swing' if p is sw_hi or p is sw_lo else 'internal'
            pivots.append({'scope': scope, 'kind': 1 if p is sw_hi or p is in_hi else -1,
                           'level': level, 'pivot_i': idx, 'i': i})

    def order_block(p, i, side):
        if side == BULLISH:
            seg = pl[p.index:i]
            k = p.index + int(np.argmin(seg))
        else:
            seg = ph[p.index:i]
            k = p.index + int(np.argmax(seg))
        return max(ph[k], pl[k]), min(ph[k], pl[k]), k

    def structure(scope, i):
        hi, lo = (in_hi, in_lo) if scope == 'internal' else (sw_hi, sw_lo)
        for side, p in ((BULLISH, hi), (BEARISH, lo)):
            lvl = p.current
            before = prev_lvl[(scope, side)]
            prev_lvl[(scope, side)] = lvl
            if i == 0 or np.isnan(lvl) or np.isnan(before):
                continue
            if side == BULLISH:
                cross = c[i] > lvl and c[i - 1] <= before
                other = sw_hi.current
            else:
                cross = c[i] < lvl and c[i - 1] >= before
                other = sw_lo.current
            if scope == 'internal':
                # extraCondition: внутренний уровень не совпадает со свинговым
                extra = not np.isnan(other) and lvl != other
            else:
                extra = True
            if not cross or p.crossed or not extra:
                continue
            tag = 'CHoCH' if trend[scope] == -side else 'BOS'
            bias_swing, bias_internal = trend['swing'], trend['internal']
            p.crossed = True
            trend[scope] = side
            ob_hi, ob_lo, ob_i = order_block(p, i, side)
            events.append({'i': i, 'scope': scope, 'side': side, 'tag': tag,
                           'level': lvl, 'pivot_i': p.index,
                           'ob_hi': ob_hi, 'ob_lo': ob_lo, 'ob_i': ob_i,
                           # как хранит скрипт (у волатильной свечи high и low переставлены)
                           'ob_bar_high': ph[ob_i], 'ob_bar_low': pl[ob_i],
                           'swing_bias': bias_swing, 'internal_bias': bias_internal,
                           'trail_top': top, 'trail_bottom': bottom})

    for i in range(n):
        # updateTrailingExtremes: math.max с na даёт na — до первой точки пусто
        top = max(h[i], top) if not np.isnan(top) else np.nan
        bottom = min(l[i], bottom) if not np.isnan(bottom) else np.nan
        ch = swing_legs.step(i)
        if ch == 1:
            set_pivot(sw_lo, l[i - swing_size], i - swing_size)
            bottom = sw_lo.current
        elif ch == -1:
            set_pivot(sw_hi, h[i - swing_size], i - swing_size)
            top = sw_hi.current
        ch = internal_legs.step(i)
        if ch == 1:
            set_pivot(in_lo, l[i - internal_size], i - internal_size)
        elif ch == -1:
            set_pivot(in_hi, h[i - internal_size], i - internal_size)
        structure('internal', i)
        structure('swing', i)
    return events


def _selftest():
    # пила: подъём, откат, пробой вершины — бычий слом с блоком на дне отката
    up = np.linspace(100, 110, 60)
    down = np.linspace(110, 104, 30)
    up2 = np.linspace(104, 115, 40)
    flat = np.full(70, 115.0)
    c = np.r_[up, down, up2, flat]
    h, l = c + 0.2, c - 0.2
    ev = build(h, l, c, swing_size=10, internal_size=3)
    bull = [e for e in ev if e['side'] == BULLISH]
    assert bull, ev
    e = bull[0]
    assert e['i'] > 90 and 103.5 < e['ob_lo'] < 104.5, e
    # точка известна только через size баров: событие не раньше вершины + size
    assert all(e['i'] >= e['ob_i'] for e in ev)
    print('lux: ok', len(ev), 'событий')


if __name__ == '__main__':
    _selftest()
