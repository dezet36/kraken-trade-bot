"""
Импульс между монетами: держится ли плюс на соседних настройках.

ЗАЧЕМ. research/ai_xsection_ml.py (27.09.2026): при недельном удержании
«купить три монеты с лучшим ходом за 30 дней, продать три худших» дало
+0.43% на позицию за неделю после издержек, в плюсе во всех четырёх
периодах (bear +0.09, mid1 +0.93, mid2 +0.60, recent +0.35). Там перебиралось
8 правил × 3 горизонта — один удачный вариант мог выйти случайно. Здесь
сетка вокруг него: окно импульса 3–90 дней, удержание 7 и 14 дней, 2–5 монет
с каждой стороны, все дни перестановки. Настоящее свойство рынка даёт плато
(соседние клетки похожи), случайность — одинокий пик.

Издержки двумя способами: «полные» — круг 0.21% на каждую позицию каждый
период (как в ai_xsection_ml); «по обороту» — платят только вошедшие и
вышедшие позиции (половина круга за вход, половина за выход), оставшиеся в
книге не платят. Фандинг — последняя выплаченная ставка × число выплат.

Наблюдение — одна книга (среднее по позициям) на точку перестановки;
интервал — бутстреп по точкам.

Запуск:
    python research/ai_momentum_grid.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_direction_ml as M                           # noqa: E402
from common import ci                                 # noqa: E402

H = M.H
STEP = 4                                               # строки кэша — каждые 4 ч
ROUND_TRIP = 0.21
ORDER = ['bear', 'mid1', 'mid2', 'recent']
LOOKBACK_D = (3, 7, 14, 21, 30, 45, 60, 90)
HOLD_D = (7, 14)
TOP_K = (2, 3, 4, 5)


def wide(data):
    closes = data.pivot_table(index='t', columns='pair', values='c')
    grid = np.arange(closes.index.min(), closes.index.max() + 1, STEP * H)
    closes = closes.reindex(grid)
    fund = data.pivot_table(index='t', columns='pair', values='fund').reindex(grid)
    fold = data.drop_duplicates('t').set_index('t')['fold'].reindex(grid).ffill()
    return closes, fund, fold


def run(closes, fund, fold, lookback_d, hold_d, k, offset_d, score=None):
    rows_back, rows_fwd = lookback_d * 24 // STEP, hold_d * 24 // STEP
    mom = closes / closes.shift(rows_back) - 1 if score is None else score
    fwd = (closes.shift(-rows_fwd) / closes - 1) * 100
    t = closes.index.to_numpy()
    pick = ((t // H) % (hold_d * 24) == offset_d * 24)
    books, prev = [], {}
    for i in np.flatnonzero(pick):
        m, f, fr = mom.iloc[i], fwd.iloc[i], fund.iloc[i]
        ok = m.notna() & f.notna()
        if ok.sum() < 10:
            prev = {}
            continue
        ranked = m[ok].sort_values()
        book = {p: -1 for p in ranked.index[:k]} | {p: +1 for p in ranked.index[-k:]}
        gross = np.mean([side * f[p] + (0.0 if np.isnan(fr[p]) else -side * fr[p] / 100 * hold_d * 3)
                         for p, side in book.items()])
        changed = sum(1 for p, s in book.items() if prev.get(p) != s) + \
            sum(1 for p, s in prev.items() if book.get(p) != s)
        turnover_cost = ROUND_TRIP / 2 * changed / len(book)
        books.append((t[i], fold.iloc[i], gross - ROUND_TRIP, gross - turnover_cost))
        prev = book
    return pd.DataFrame(books, columns=['t', 'fold', 'full', 'turn'])


def summarize(frames, column):
    per_offset = [f[column].mean() for f in frames if len(f)]
    per_fold = {fo: np.mean([f[f.fold == fo][column].mean() for f in frames if (f.fold == fo).any()])
                for fo in ORDER}
    return np.mean(per_offset), min(per_offset), max(per_offset), per_fold


def main():
    data = M.load_all()
    closes, fund, fold = wide(data)
    print(f'пар {closes.shape[1]}, точек сетки {len(closes)}; издержки: полные {ROUND_TRIP}% на позицию '
          f'за период / по обороту; фандинг включён')
    for hold_d in HOLD_D:
        print(f'\n== УДЕРЖАНИЕ {hold_d} дн — среднее по {hold_d} дням перестановки, % на позицию за период')
        print(f'   {"окно":>6s} {"k":>2s}  {"полные":>8s}  {"по обороту":>10s}  {"разброс дней (оборот)":>22s}  '
              f'периоды по обороту: bear | mid1 | mid2 | recent   в плюс')
        for lookback_d in LOOKBACK_D:
            for k in TOP_K:
                frames = [run(closes, fund, fold, lookback_d, hold_d, k, off) for off in range(hold_d)]
                full = summarize(frames, 'full')
                turn = summarize(frames, 'turn')
                pos = sum(1 for v in turn[3].values() if v > 0)
                print(f'   {lookback_d:4d} д {k:2d}  {full[0]:+8.3f}  {turn[0]:+10.3f}  '
                      f'{turn[1]:+8.3f} … {turn[2]:+8.3f}   '
                      + ' | '.join(f'{turn[3][fo]:+.3f}' for fo in ORDER) + f'   {pos} из 4', flush=True)
    # Интервал для центральной клетки — по книгам одного дня перестановки.
    print('\nЦЕНТРАЛЬНАЯ КЛЕТКА (30 дн, удержание 7 дн, k=3), по обороту, интервал по неделям:')
    for off in range(7):
        f = run(closes, fund, fold, 30, 7, 3, off)
        lo, hi = ci(f['turn'].to_numpy())
        print(f'   день {off}: {len(f)} недель, {f["turn"].mean():+.3f}% [{lo:+.3f}; {hi:+.3f}], '
              f'в плюс {np.mean(f["turn"] > 0) * 100:.0f}% недель')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
