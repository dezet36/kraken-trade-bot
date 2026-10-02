"""Исполнение заявок по минуткам — консервативно, с издержками Bybit.

Заявка: сторона, минута постановки, минута снятия (если не налилась), цена
входа (nan — по рынку на открытии минуты постановки), стоп, цель, предел
удержания в минутах. Стоп не двигается.

Правила (см. PROTOCOL.md):
- лимит наливается, только если цена прошла сквозь него (low < вход для
  покупки); если рынок уже за лимитом в минуту постановки — налив по рынку
  (тейкер);
- цель задета раньше налива — заявка снимается (сделка упущена); если в
  одной минуте и налив, и цель — тоже снимается;
- в минуту налива проверяется только стоп; если в минуте задеты и стоп, и
  цель — стоп;
- стоп исполняется по худшей из цен: стоп или открытие минуты (гэп).
"""
from __future__ import annotations

import numpy as np
from numba import njit

MAKER = 0.0002
TAKER = 0.00055
SLIP_STOP = 0.0005
SLIP_MKT = 0.0002
FUND_8H = 0.0001

ST_EXPIRED, ST_FILLED, ST_MISSED, ST_INVALID = 0, 1, 2, 3
EX_NONE, EX_STOP, EX_TARGET, EX_TIME = 0, 1, 2, 3


@njit(cache=True)
def simulate(o, h, l, c, side, i_place, i_expire, entry, stop, target, hold,
             cost_mult, i_deadline, cumf):
    n = len(side)
    N = len(o)
    status = np.zeros(n, np.int64)
    fill_i = np.full(n, -1, np.int64)
    end_i = np.full(n, -1, np.int64)
    ex_type = np.zeros(n, np.int64)
    fill_px = np.full(n, np.nan)
    exit_px = np.full(n, np.nan)
    r_gross = np.full(n, np.nan)
    r_net = np.full(n, np.nan)
    fee_r = np.full(n, np.nan)
    fund_r = np.full(n, np.nan)
    for q in range(n):
        s = side[q]
        e = entry[q]
        st = stop[q]
        tg = target[q]
        i0 = i_place[q]
        if i0 >= N:
            status[q] = ST_INVALID
            end_i[q] = N - 1
            continue
        f = -1
        fpx = np.nan
        fee_in = 0.0
        if np.isnan(e) or (s == 1 and o[i0] <= e) or (s == -1 and o[i0] >= e):
            f = i0
            fpx = o[i0]
            fee_in = (TAKER + SLIP_MKT) * cost_mult
        else:
            ie = min(i_expire[q], N)
            for i in range(i0, ie):
                if s == 1:
                    hit_t = h[i] >= tg
                    hit_e = l[i] < e
                else:
                    hit_t = l[i] <= tg
                    hit_e = h[i] > e
                if hit_t:
                    status[q] = ST_MISSED
                    end_i[q] = i
                    break
                if hit_e:
                    f = i
                    fpx = e
                    fee_in = MAKER * cost_mult
                    break
            if f < 0:
                if status[q] != ST_MISSED:
                    status[q] = ST_EXPIRED
                    end_i[q] = ie - 1
                continue
        risk = (fpx - st) * s
        if risk <= 0 or (tg - fpx) * s <= 0:
            status[q] = ST_INVALID
            end_i[q] = f
            continue
        status[q] = ST_FILLED
        fill_i[q] = f
        fill_px[q] = fpx
        xi = -1
        xpx = np.nan
        xt = EX_NONE
        fee_out = 0.0
        # минута налива: только стоп
        if (s == 1 and l[f] <= st) or (s == -1 and h[f] >= st):
            xi = f
            xpx = st
            xt = EX_STOP
            fee_out = (TAKER + SLIP_STOP) * cost_mult
        else:
            last = min(f + hold[q], N - 1, i_deadline[q])
            if last < f:
                last = f
            for i in range(f + 1, last + 1):
                if s == 1:
                    if l[i] <= st:
                        xi = i
                        xpx = min(st, o[i])
                        xt = EX_STOP
                        break
                    if h[i] > tg:
                        xi = i
                        xpx = max(tg, o[i]) if o[i] > tg else tg
                        xt = EX_TARGET
                        break
                else:
                    if h[i] >= st:
                        xi = i
                        xpx = max(st, o[i])
                        xt = EX_STOP
                        break
                    if l[i] < tg:
                        xi = i
                        xpx = min(tg, o[i]) if o[i] < tg else tg
                        xt = EX_TARGET
                        break
            if xt == EX_STOP:
                fee_out = (TAKER + SLIP_STOP) * cost_mult
            elif xt == EX_TARGET:
                fee_out = MAKER * cost_mult
            else:
                xi = last
                xpx = c[last]
                xt = EX_TIME
                fee_out = (TAKER + SLIP_MKT) * cost_mult
        end_i[q] = xi
        ex_type[q] = xt
        exit_px[q] = xpx
        g = (xpx - fpx) * s / risk
        # фандинг: сумма ставок расчётов, пока позиция открыта; покупатель
        # платит положительную ставку, продавец получает
        fund = s * (cumf[xi] - cumf[f])
        fee = (fee_in * fpx + fee_out * xpx) / risk
        fr = fund * fpx / risk
        r_gross[q] = g
        fee_r[q] = fee
        fund_r[q] = fr
        r_net[q] = g - fee - fr
    return status, fill_i, end_i, ex_type, fill_px, exit_px, r_gross, r_net, fee_r, fund_r


@njit(cache=True)
def one_at_a_time(i_place, end_i, status):
    """Одна заявка/позиция на пару: новая не ставится, пока прежняя жива.
    Заявки должны идти по возрастанию i_place."""
    n = len(i_place)
    keep = np.zeros(n, np.bool_)
    busy = -1
    for q in range(n):
        if status[q] == ST_INVALID:
            continue
        if i_place[q] > busy:
            keep[q] = True
            busy = end_i[q]
    return keep


def const_funding(m1):
    """Запасной фандинг, если истории нет: 0.01% за 8 ч не в пользу —
    для покупок и продаж одинаково это не выразить суммой, поэтому
    возвращается ряд для покупки; run() берёт модуль для продаж."""
    return np.arange(len(m1.o)) * (FUND_8H / 480.0)


def run(m1, orders: dict, cost_mult=1.0, exclusive=True, cumf=None):
    """orders — словарь массивов: side, i_place, i_expire, entry, stop,
    target, hold [, i_deadline — минутка, на закрытии которой позиция
    закрывается в любом случае: встречный слом структуры].
    Возвращает словарь с итогами по каждой заявке."""
    order = np.argsort(orders['i_place'], kind='stable')
    od = {k: np.asarray(v)[order] for k, v in orders.items()}
    if 'i_deadline' not in od:
        od['i_deadline'] = np.full(len(od['side']), len(m1.o) - 1, np.int64)
    res = simulate(m1.o, m1.h, m1.l, m1.c,
                   od['side'].astype(np.int64), od['i_place'].astype(np.int64),
                   od['i_expire'].astype(np.int64), od['entry'].astype(np.float64),
                   od['stop'].astype(np.float64), od['target'].astype(np.float64),
                   od['hold'].astype(np.int64), float(cost_mult),
                   od['i_deadline'].astype(np.int64),
                   cumf if cumf is not None else const_funding(m1))
    names = ('status', 'fill_i', 'end_i', 'ex_type', 'fill_px', 'exit_px',
             'r_gross', 'r_net', 'fee_r', 'fund_r')
    out = dict(od)
    out.update(dict(zip(names, res)))
    if cumf is None:
        # запасной фандинг списывается всегда, и продажам тоже
        neg = out['side'] == -1
        out['fund_r'] = np.where(neg, -out['fund_r'], out['fund_r'])
        out['r_net'] = out['r_gross'] - out['fee_r'] - out['fund_r']
    if exclusive:
        keep = one_at_a_time(od['i_place'].astype(np.int64), out['end_i'],
                             out['status'])
    else:
        keep = out['status'] != ST_INVALID
    out['keep'] = keep
    return out


def _selftest():
    # цена: 100 → падение до 99 (налив покупки 99.5) → рост до 102 (цель 101)
    c = np.array([100, 100, 99.8, 99.4, 99.0, 99.6, 100.5, 101.2, 101.5, 101.0])
    o = np.r_[100, c[:-1]]
    h = np.maximum(o, c) + 0.05
    l = np.minimum(o, c) - 0.05
    od = dict(side=np.array([1, 1, -1]), i_place=np.array([1, 1, 1]),
              i_expire=np.array([9, 9, 9]), entry=np.array([99.5, 99.5, 100.6]),
              stop=np.array([98.9, 99.3, 101.6]), target=np.array([101.0, 101.0, 99.0]),
              hold=np.array([100, 100, 100]))
    r = simulate(o, h, l, c, od['side'], od['i_place'], od['i_expire'], od['entry'],
                 od['stop'], od['target'], od['hold'], 1.0, np.full(3, 10**9, np.int64),
                 np.zeros(len(o)))
    status, fill_i, end_i, ex_type, fpx, xpx, rg, rn, _, _ = r
    assert status[0] == ST_FILLED and ex_type[0] == EX_TARGET, r
    assert abs(rg[0] - (101.0 - 99.5) / 0.6) < 1e-9
    assert ex_type[1] == EX_STOP and xpx[1] == 99.3, r
    # продажа 100.6: цена ушла к цели 99.0 раньше, чем дошла до входа
    assert status[2] == ST_MISSED, r
    assert rn[0] < rg[0]
    print('sim: ok')


if __name__ == '__main__':
    _selftest()
