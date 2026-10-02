"""Прогон: события SMC по паре × геометрии заявок → итог каждой сделки.

    python -m smcz.run_events --src bybit --tag v1 [--pairs BTCUSDT ...]

Пишет research/results/smcz/{tag}/{PAIR}.pkl:
  {'ev': {(tf,k): DataFrame событий с признаками},
   'res': {(tf,k,zone,target,expiry,hold): DataFrame итогов по событиям}}
Исключение «одна позиция на пару» здесь НЕ применяется — оно зависит от
среза (фильтров) и делается при разборе (smcz.analyze).
"""
from __future__ import annotations

import argparse
import itertools
import os
import pickle
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

from smcz import data as D
from smcz import events as E
from smcz import sim

OUT_ROOT = os.path.join(D.ROOT, 'results', 'smcz')

# сетки: (tf, k) — рабочий ТФ и размер свинга; удержание — в часах
GRIDS = {
    'g1': dict(struct=[('15m', 3), ('15m', 5), ('1h', 3), ('1h', 5), ('4h', 3)],
               zones=['obw', 'obm', 'fvg', 'fvgm', 'mkt'],
               targets=['ext', 1.5, 2.0, 3.0, 5.0], expiry=[24], hold_h=None),
    # старшие ТФ: где живёт плюс, сколько держать, какая цель
    'g2': dict(struct=[('2h', 3), ('4h', 2), ('4h', 3), ('4h', 5), ('6h', 3),
                       ('8h', 3), ('12h', 2), ('12h', 3), ('1d', 2), ('1d', 3)],
               zones=['obw', 'fvg', 'mkt'],
               targets=[1.5, 2.0, 3.0, 5.0, 8.0], expiry=[12], hold_h=[240, 720]),
    # старшие ТФ, вход на сломе или ретестом уровня; цель — R, ликвидность
    # или без цели; выход по встречному слому — да/нет
    # кандидаты C1–C3 и их соседи — для перемера с фактическим фандингом
    'c1': dict(struct=[('4h', 3), ('8h', 3)], zones=['mkt'],
               targets=[8.0, 'none'], expiry=[6], hold_h=[720], choch=[False, True]),
    'g3': dict(struct=[('4h', 3), ('6h', 3), ('8h', 3), ('12h', 3), ('1d', 2)],
               zones=['mkt', 'lvl'],
               targets=[3.0, 5.0, 8.0, 'liq', 'liq2', 'none'], expiry=[6],
               hold_h=[720, 2160], choch=[False, True]),
}


def run_pair(args):
    pair, src, tag, grid = args
    struct, zones, targets, expiry = grid['struct'], grid['zones'], grid['targets'], grid['expiry']
    out = os.path.join(OUT_ROOT, tag, f'{pair}.pkl')
    if os.path.exists(out):
        return pair, 'уже есть'
    t0 = time.time()
    bars = D.load(pair, src=src)
    m1 = bars['base']
    cumf = D.funding_cum(pair, 'binance' if src == 'bv1h' else src, m1.t)
    if cumf is None and src == 'bv1h':
        return pair, 'нет фандинга — позже'
    res = {}
    evs = {}
    for tf, k in struct:
        ev = E.build(bars, tf, k)
        evs[(tf, k)] = ev
        B = bars[tf]
        # удержание: в барах рабочего ТФ (g1 — 96 баров) или в часах
        hold = [96] if grid['hold_h'] is None else [max(1, h * 60 // B.tf) for h in grid['hold_h']]
        for zone, tg, ex, ho, ch in itertools.product(zones, targets, expiry, hold,
                                                      grid.get('choch', [None])):
            od = E.orders(ev, B, zone, tg, ex, ho, exit_choch=bool(ch), unit=m1.tf)
            valid = (od['risk'] > 0) & ((od['target'] - od['ref']) * od['side'] > 0)
            if zone != 'mkt':
                valid &= ~np.isnan(od['entry'])
            odv = {kk: v[valid] for kk, v in od.items()}
            r = sim.run(m1, odv, exclusive=False, cumf=cumf)
            # путь сделки от ставки издержек не зависит: ×1.5 — линейно по
            # комиссиям и проскальзыванию; фандинг — фактический
            df = pd.DataFrame({
                'ev': r['ev'], 'i_place': r['i_place'],
                'status': r['status'].astype(np.int8),
                'ex_type': r['ex_type'].astype(np.int8),
                'fill_i': r['fill_i'], 'end_i': r['end_i'],
                'stop_pct': (np.abs(r['ref'] - r['stop']) / r['ref']).astype(np.float32),
                'rr': (np.abs(r['target'] - r['ref']) / r['risk']).astype(np.float32),
                'r_gross': r['r_gross'].astype(np.float32),
                'r_net': r['r_net'].astype(np.float32),
                'r_net15': (r['r_gross'] - 1.5 * r['fee_r'] - r['fund_r']).astype(np.float32),
                'fund_r': r['fund_r'].astype(np.float32),
            })
            key = (tf, k, zone, tg, ex, ho) + (() if ch is None else (ch,))
            res[key] = df
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump({'ev': evs, 'res': res, 't1': m1.t[0], 'unit': m1.tf,
                     'funding': cumf is not None},
                    f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, out)
    return pair, f'{time.time() - t0:.0f} с'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default='bybit')
    ap.add_argument('--tag', required=True)
    ap.add_argument('--pairs', nargs='*')
    ap.add_argument('--procs', type=int, default=4)
    ap.add_argument('--grid', default='g1')
    a = ap.parse_args()
    pairs = a.pairs or {'bybit': D.bybit_pairs, 'bv1h': D.bv_pairs}.get(a.src, lambda: D.PAIRS)()
    jobs = [(p, a.src, a.tag, GRIDS[a.grid]) for p in pairs]
    with Pool(a.procs) as pool:
        for pair, msg in pool.imap_unordered(run_pair, jobs):
            print(pair, msg, flush=True)


if __name__ == '__main__':
    main()
