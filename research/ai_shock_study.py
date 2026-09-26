"""
Продолжение после объёмного выноса: исполнение по 5-минуткам.

ЗАЧЕМ. ai_signals_study показал: после часовой свечи с ходом больше 3 ATR на
объёме больше 3 медиан ставка ПРОТИВ хода теряет во всех четырёх периодах за
4 часа (−0.24% [−0.36; −0.12]), то есть по ходу — плюс до издержек. Там вход
считался по закрытию свечи-события. Здесь — как было бы в жизни: вход по
открытию первой 5-минутки после закрытия часа, тейкер и проскальзывание на
входе и выходе, выход по времени; вариант — со стопом за серединой свечи-
события (ход отменён — идеи нет).

Пороги — сетка круглых значений, чтобы видеть, держится ли знак, а не выбирать
лучший: ATR ×2.5/3/4, объём ×2/3/4.

Запуск:
    python research/ai_shock_study.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_doctrine as D                               # noqa: E402
from common import ci                                 # noqa: E402

H = 3_600_000
FEE_T, SLIP = 0.00055, 0.0005
FOLDS = {'bear': 'bear', 'mid1': 'mid1', 'mid2': 'mid2', '12m': 'recent', 'fresh': 'recent'}


_data = {}


def pair_data(period, pair):
    """Свечи и готовые ряды пары: ход часа, ATR прошлого часа, медиана объёма 48 прошлых."""
    key = (period, pair)
    if key not in _data:
        cache = D.PERIODS[period]
        a1 = D.load(cache, pair, '1h')
        a5 = D.load(cache, pair, '5m')
        if a1 is None or a5 is None:
            _data[key] = None
        else:
            c, v = a1[:, 4], a1[:, 5]
            move = np.full(len(c), np.nan)
            move[1:] = (c[1:] / c[:-1] - 1) * 100
            atr_prev = np.full(len(c), np.nan)
            atr_prev[1:] = D.atr_pct(a1)[:-1]
            med_prev = pd.Series(v).rolling(48).median().shift(1).to_numpy()
            _data[key] = (a1, a5, move, atr_prev, med_prev)
    return _data[key]


def events(period, atr_k, vol_k, hold_h, with_stop):
    out = []
    for pair in D.PAIRS:
        got = pair_data(period, pair)
        if got is None:
            continue
        a1, a5, move, atr_prev, med_prev = got
        c, o, v = a1[:, 4], a1[:, 1], a1[:, 5]
        ts5 = a5[:, 0]
        hits = np.flatnonzero((np.abs(move) > atr_k * atr_prev) & (med_prev > 0) & (v > vol_k * med_prev))
        busy_until = -1
        for i in hits:
            if i < 60 or i >= len(a1) - hold_h - 1 or a1[i, 0] < busy_until:
                continue
            side = 1 if move[i] > 0 else -1
            t_in = a1[i, 0] + H
            k = int(np.searchsorted(ts5, t_in))
            end = int(np.searchsorted(ts5, t_in + hold_h * H))
            if k >= len(a5) or end >= len(a5) or end <= k:
                continue
            entry = a5[k, 1] * (1 + SLIP * side)
            exit_px = a5[end - 1, 4]
            if with_stop:
                stop = (o[i] + c[i]) / 2                 # середина свечи-события
                for j in range(k, end):
                    if (a5[j, 3] <= stop) if side > 0 else (a5[j, 2] >= stop):
                        exit_px = stop
                        break
            exit_px *= (1 - SLIP * side)
            gross = (exit_px / entry - 1) * 100 * side
            net = gross - FEE_T * 100 * 2
            out.append({'period': period, 'pair': pair, 't': a1[i, 0], 'net': net})
            busy_until = t_in + hold_h * H
    return out


def main():
    print('Продолжение после объёмного выноса, вход по следующей 5m, выход по времени; чистыми, % цены')
    print(f'{"ATR×":>5s} {"объём×":>6s} {"часы":>4s} {"стоп":>5s} | ' + ' '.join(f'{f:>16s}' for f in ('bear', 'mid1', 'mid2', 'recent'))
          + ' | все [95%]')
    for atr_k in (2.5, 3.0, 4.0):
        for vol_k in (2.0, 3.0, 4.0):
            for hold_h in (4, 12):
                for with_stop in (False, True):
                    rows = []
                    for period in D.PERIODS:
                        rows += events(period, atr_k, vol_k, hold_h, with_stop)
                    f = pd.DataFrame(rows)
                    if f.empty:
                        continue
                    f['fold'] = f.period.map(FOLDS)
                    f = f.drop_duplicates(subset=['pair', 't'])
                    cells = [f'{f[f.fold == fold].net.mean():+7.3f}% ({(f.fold == fold).sum():4d})'
                             for fold in ('bear', 'mid1', 'mid2', 'recent')]
                    lo, hi = ci(f.net.to_numpy())
                    print(f'{atr_k:5.1f} {vol_k:6.1f} {hold_h:4d} {"да" if with_stop else "нет":>5s} | ' + ' '.join(cells)
                          + f' | {f.net.mean():+.3f}% [{lo:+.3f}; {hi:+.3f}]', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
