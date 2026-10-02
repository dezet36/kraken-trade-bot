"""Портфель: общий счёт, риск на сделку, предел одновременных позиций.

Сделки (уже прошедшие «одна позиция на пару») идут по времени налива.
Если все места заняты — сделка пропускается. Риск — доля текущего капитала
(сложный процент) или начального. Капитал меняется на выходе (r_net × риск).
Просадка считается по закрытым сделкам и по дневной переоценке открытых
позиций (по дневным закрытиям пары), если переданы цены.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def run(tr: pd.DataFrame, risk=0.01, slots=6, compound=True, start=10_000.0,
        max_same_side=None):
    """tr: колонки pair, t_in, t_out (минуты), r_net, side.
    max_same_side — предел позиций в одну сторону (None — без предела)."""
    tr = tr.sort_values('t_in', kind='stable').reset_index(drop=True)
    eq = start
    open_pos = []           # (t_out, risk_amount, r_net, side, idx)
    taken = np.zeros(len(tr), bool)
    curve = []
    for i, row in enumerate(tr.itertuples(index=False)):
        # закрыть всё, что вышло до этого входа
        open_pos.sort(key=lambda p: p[0])
        while open_pos and open_pos[0][0] <= row.t_in:
            t_out, amt, r, side, _ = open_pos.pop(0)
            eq += amt * r
            curve.append((t_out, eq))
        if len(open_pos) >= slots:
            continue
        if max_same_side is not None and sum(1 for p in open_pos if p[3] == row.side) >= max_same_side:
            continue
        amt = risk * (eq if compound else start)
        open_pos.append((row.t_out, amt, row.r_net, row.side, i))
        taken[i] = True
    for t_out, amt, r, side, _ in sorted(open_pos, key=lambda p: p[0]):
        eq += amt * r
        curve.append((t_out, eq))
    c = pd.DataFrame(curve, columns=['t', 'eq'])
    return tr[taken].copy(), c


def summary(c: pd.DataFrame, start=10_000.0):
    if c.empty:
        return dict(final=start, ret=0, mdd=0)
    eq = np.r_[start, c['eq'].to_numpy()]
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    years = (c['t'].iloc[-1] - c['t'].iloc[0]) / (1440 * 365.25)
    final = eq[-1]
    cagr = (final / start) ** (1 / years) - 1 if years > 0.2 and final > 0 else np.nan
    return dict(final=round(final, 0), ret=round(final / start - 1, 3),
                cagr=round(cagr, 3) if cagr == cagr else np.nan, mdd=round(dd.min(), 3),
                years=round(years, 2))


def by_year(c: pd.DataFrame, start=10_000.0):
    if c.empty:
        return pd.Series(dtype=float)
    s = c.set_index(pd.to_datetime(c['t'] * 60, unit='s', utc=True))['eq']
    y = s.groupby(s.index.year).last()
    prev = y.shift(1)
    prev.iloc[0] = start
    return (y / prev - 1).round(3)


def run_mtm(tr: pd.DataFrame, closes: dict, risk=0.005, slots=10**6, max_same_side=None,
            start=10_000.0, compound=True):
    """Тот же отбор сделок, но капитал — по дневной переоценке открытых
    позиций (закрытие дня UTC). closes: pair → pd.Series(закрытие, индекс —
    номер дня). Риск в деньгах фиксируется при входе.
    Колонки tr: pair, t_in, t_out, r_net, side, fill_px, stop."""
    tr = tr.sort_values('t_in', kind='stable').reset_index(drop=True)
    d0 = int(tr.t_in.min() // 1440)
    d1 = int(tr.t_out.max() // 1440) + 1
    eq_real = start
    open_pos = []
    day_eq = []
    i = 0
    n = len(tr)
    for day in range(d0, d1 + 1):
        t_end = (day + 1) * 1440
        # события дня по порядку: выходы и входы
        while True:
            nxt_in = tr.t_in.iat[i] if i < n else None
            nxt_out = min((p['t_out'] for p in open_pos), default=None)
            cand = [x for x in (nxt_in, nxt_out) if x is not None and x < t_end]
            if not cand:
                break
            if nxt_out is not None and nxt_out < t_end and (nxt_in is None or nxt_out <= nxt_in):
                p = min(open_pos, key=lambda q: q['t_out'])
                open_pos.remove(p)
                eq_real += p['amt'] * p['r']
                continue
            row = tr.iloc[i]
            i += 1
            if len(open_pos) >= slots:
                continue
            if max_same_side is not None and sum(1 for p in open_pos if p['side'] == row.side) >= max_same_side:
                continue
            amt = risk * (eq_real if compound else start)
            open_pos.append(dict(pair=row.pair, t_out=row.t_out, amt=amt, r=row.r_net,
                                 side=row.side, entry=row.fill_px,
                                 riskpx=abs(row.fill_px - row.stop)))
        unreal = 0.0
        for p in open_pos:
            s = closes.get(p['pair'])
            if s is None or day not in s.index:
                continue
            unreal += p['amt'] * p['side'] * (s.at[day] - p['entry']) / p['riskpx']
        day_eq.append((day, eq_real + unreal))
    c = pd.DataFrame(day_eq, columns=['day', 'eq'])
    c['t'] = c.day * 1440
    return c
