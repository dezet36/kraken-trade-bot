"""
ФИБО: направление по 4ч, сетап по импульсу часа — идея владельца, проверка
D1–D3 (docs/Аудит_сделок_2026-10-07.md, раздел 13; протокол закоммичен до
расчёта, 0f73c54).

    D1  EMA строго: лонг при BULLISH, шорт при BEARISH, NEUTRAL не торгуется;
    D2  структура 4ч: сторона последнего слома на закрытых 4ч (фрактал 3+3,
        smcs.core.market_structure);
    D3  4ч-цена относительно EMA50: выше — лонги, ниже — шорты.

База — ФИБО как в боте, обе стороны, без толпы. Заявки — боевой сканер
(fibo_live_sim / fibo_other_pairs / 2021), уже прошедшие фильтр сканера
(нет лонгов при BEARISH и шортов при BULLISH): D2 и D3 здесь — внутри них.
EMA — по 4ч с формирующейся свечой, как сканер; структура — только по
закрытым 4ч.

Приёмка: R на сделку выше базы во всех 9 ячейках И среднее > 0 при наливе
«насквозь» на всех вместе.

    python research/fibo_4h_direction.py   → results/fibo_4h_direction.txt
"""
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fibo_live_sim as FS                            # noqa: E402
import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
import config                                         # noqa: E402
import smc_engine                                     # noqa: E402
from common import ci                                 # noqa: E402
from fibo_crowd import funding_at                     # noqa: E402
from fibo_geometry import CELLS, load_cell            # noqa: E402
from fibo_htf_split import BASE, N4                   # noqa: E402
from strategies.smcs import core as smcs_core                    # noqa: E402

RULES = [
    ('база: ФИБО обе стороны (как сканер)', lambda o, x: True),
    ('D1 EMA строго (без NEUTRAL)', lambda o, x: x['ema'] == ('BULLISH' if o.direction == 'LONG' else 'BEARISH')),
    ('D2 структура 4ч (последний слом)', lambda o, x: x['struct'] == (1 if o.direction == 'LONG' else -1)),
    ('D3 4ч-цена и EMA50', lambda o, x: x['above50'] == (o.direction == 'LONG')),
    ('база и против толпы (для понимания)', lambda o, x: x['crowd']),
    ('D1 и против толпы (для понимания)', lambda o, x: x['crowd'] and x['ema'] == ('BULLISH' if o.direction == 'LONG' else 'BEARISH')),
    ('D2 и против толпы (для понимания)', lambda o, x: x['crowd'] and x['struct'] == (1 if o.direction == 'LONG' else -1)),
    ('D3 и против толпы (для понимания)', lambda o, x: x['crowd'] and x['above50'] == (o.direction == 'LONG')),
]


def features(orders, data):
    """EMA-тренд (как сканер), цена/EMA50 и структура 4ч на момент каждой заявки."""
    h4, m5 = data['4h'], data['5m']
    t4 = FS.naive(h4['timestamp'])
    t5 = FS.naive(m5['timestamp'])
    o4, hi4, lo4, c4 = (h4[c].to_numpy(dtype=float) for c in ('open', 'high', 'low', 'close'))
    c5 = m5['close'].to_numpy(dtype=float)
    struct = np.array(smcs_core.market_structure(o4.tolist(), hi4.tolist(), lo4.tolist(), c4.tolist(), 3)[0])
    a_fast, a_slow = 2 / (config.HTF_EMA_FAST + 1), 2 / (config.HTF_EMA_SLOW + 1)
    out = {}
    for o in orders:
        now = np.datetime64(pd.Timestamp(o.created).tz_localize(None) if pd.Timestamp(o.created).tzinfo
                            else pd.Timestamp(o.created))
        hour = np.datetime64(pd.Timestamp(now - np.timedelta64(1, 'm')).floor('h'))
        k4 = int(np.searchsorted(t4, hour, side='right')) - 1
        j = int(np.searchsorted(t5, now, side='left'))
        if k4 < 1 or j < 1:
            out[id(o)] = {'ema': 'NEUTRAL', 'above50': None, 'struct': 0}
            continue
        closes = np.append(c4[max(0, k4 - N4 + 1):k4], c5[j - 1])
        if len(closes) < config.HTF_EMA_SLOW + 10:
            ema = 'NEUTRAL'
            above = None
        else:
            ef = pd.Series(closes).ewm(alpha=a_fast, adjust=False).mean().iloc[-1]
            es = pd.Series(closes).ewm(alpha=a_slow, adjust=False).mean().iloc[-1]
            last = closes[-1]
            ema = 'BULLISH' if last > ef > es else 'BEARISH' if last < ef < es else 'NEUTRAL'
            above = bool(last > ef)
        out[id(o)] = {'ema': ema, 'above50': above, 'struct': int(struct[k4 - 1])}
    return out


def run_cell(cell):
    cache, orders, pairs = load_cell(cell)
    orders = [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT]
    bt.CACHE_DIR = os.path.join(HERE, cache)
    data = {p: d for p in pairs if (d := bt.load_pair(p)) is not None}
    exec_data = {p: d['5m'] for p, d in data.items()}
    fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in pairs}
    feat = {}
    for p in pairs:
        if p in data:
            feat.update(features([o for o in orders if o.pair == p], data[p]))
    for o in orders:
        t = int(pd.Timestamp(o.created).value // 10 ** 6)
        f = funding_at(fund.get(o.pair), t) * (1.0 if o.direction == 'LONG' else -1.0)
        feat[id(o)]['crowd'] = bool(f <= 0)
    # проверка: заявки прошли фильтр сканера — EMA-тренд не против стороны
    bad = sum(1 for o in orders if feat[id(o)]['ema'] == ('BEARISH' if o.direction == 'LONG' else 'BULLISH'))
    mix = pd.Series([f"{feat[id(o)]['struct']:+d}/{o.direction}" for o in orders]).value_counts().to_dict()
    res = {}
    for name, rule in RULES:
        pool = [o for o in orders if rule(o, feat[id(o)])]
        for through in (0.0, 0.0005):
            smc_engine.FILL_THROUGH_PCT = through
            try:
                out = smc_engine.run_portfolio(pool, exec_data, **BASE)
            finally:
                smc_engine.FILL_THROUGH_PCT = 0.0
            res[(name, through)] = np.array([t['pnl'] / t['risk'] for t in out['trades']])
    print(f'   {cell}: заявок {len(orders)}, против EMA-фильтра сканера {bad}, структура/сторона {mix}', flush=True)
    return cell, res


def mean(a):
    return float(a.mean()) if len(a) else float('nan')


def main():
    with Pool(min(len(CELLS), os.cpu_count() or 2)) as pool:
        res = dict(pool.imap_unordered(run_cell, CELLS))
    print('\nR на сделку (касание; в скобках сделок) по ячейкам, затем всё вместе [95%] касание / насквозь')
    print(f'{"":44s} ' + ' '.join(f'{g[:3]}-{p:4s}' for g, p in CELLS))
    for name, _ in RULES:
        cells = ' '.join(f'{mean(res[c][(name, 0.0)]):+.3f}' for c in CELLS)
        allr = np.concatenate([res[c][(name, 0.0)] for c in CELLS])
        allt = np.concatenate([res[c][(name, 0.0005)] for c in CELLS])
        lo, hi = ci(allr)
        print(f'{name:44s} {cells}\n{"":44s} сделок {" ".join(str(len(res[c][(name, 0.0)])).rjust(8) for c in CELLS)}'
              f'\n{"":44s} вместе {len(allr)} сд {allr.sum():+.1f}R {mean(allr):+.3f} [{lo:+.3f}; {hi:+.3f}] | '
              f'насквозь {allt.sum():+.1f}R {mean(allt):+.3f}')
    print('\nПРИЁМКА (база — ФИБО обе стороны)')
    base = RULES[0][0]
    for name, _ in RULES[1:4]:
        better = [mean(res[c][(name, 0.0)]) > mean(res[c][(base, 0.0)]) for c in CELLS]
        allt = np.concatenate([res[c][(name, 0.0005)] for c in CELLS])
        ok = all(better) and mean(allt) > 0
        print(f'  {name}: лучше базы в {sum(better)} из {len(CELLS)} ячеек; насквозь {mean(allt):+.3f} → '
              f'{"ПРИНЯТО" if ok else "не принято"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
