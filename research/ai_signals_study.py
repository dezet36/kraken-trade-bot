"""
Сигналы позиционирования как самостоятельные стратегии: есть ли в них ход,
который перекрывает издержки.

ЗАЧЕМ. Фильтр по признакам (ai_filter_study, ai_fibo_filter) устойчиво
улучшает сетапы Фибо, и самый стойкий признак — фандинг против толпы. Но
улучшение выводит Фибо только в ноль. Если в позиционировании есть свой
ход, его можно торговать напрямую — это то, что ИИ видит в разметке и чего
не видят свечные стратегии.

Три сигнала, без подбора порогов (пороги — круглые, заданы заранее):
  1. крайний фандинг: ставка в верхних/нижних 5% своей пары за 90 дней до
     точки → позиция ПРОТИВ толпы;
  2. вынос с ликвидациями (прокси по свечам): часовая свеча с ходом больше
     3 ATR на объёме больше 3 медиан → позиция против хода свечи;
  3. суточный импульс между парами: верхние 3 по ходу за сутки минус нижние 3.

Ход вперёд — 4, 24 и 72 ч, в % цены, в сторону позиции. Издержки круга:
тейкер 0.11%, мейкер+тейкер 0.075% (для пары лонг+шорт — вдвое).
События одной пары ближе горизонта не перекрываются: следующее берётся
только после выхода предыдущего.

Запуск:
    python research/ai_signals_study.py
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
FOLDS = {'bear': ['bear'], 'mid1': ['mid1'], 'mid2': ['mid2'], 'recent': ['12m', 'fresh']}
HORIZONS = (4, 24, 72)


def forward(c, i, h):
    return (c[i + h] / c[i] - 1) * 100 if i + h < len(c) else np.nan


def funding_events(period):
    cache = D.PERIODS[period]
    out = []
    for pair in D.PAIRS:
        a1 = D.load(cache, pair, '1h')
        fr = D.load_funding(cache, pair)
        if a1 is None or fr is None:
            continue
        ts, rate = fr
        c = a1[:, 4]
        busy_until = -1
        for k in range(270, len(ts)):                  # 90 дней по 3 выплаты
            window = rate[k - 270:k]
            hi, lo = np.quantile(window, 0.95), np.quantile(window, 0.05)
            side = -1 if rate[k] > hi and rate[k] > 0.0001 else (1 if rate[k] < lo and rate[k] < 0.0001 else 0)
            if side == 0 or ts[k] < busy_until:
                continue
            i = int(np.searchsorted(a1[:, 0], ts[k], side='left'))  # первая часовая после выплаты
            if i >= len(a1) - 72 or i < 1:
                continue
            row = {'period': period, 'pair': pair, 't': ts[k], 'side': side}
            for h in HORIZONS:
                row[f'f{h}'] = forward(c, i - 1, h) * side
            out.append(row)
            busy_until = ts[k] + 72 * H
    return out


def cascade_events(period):
    cache = D.PERIODS[period]
    out = []
    for pair in D.PAIRS:
        a1 = D.load(cache, pair, '1h')
        if a1 is None:
            continue
        c, v = a1[:, 4], a1[:, 5]
        atr = D.atr_pct(a1)
        busy_until = -1
        for i in range(60, len(a1) - 72):
            if a1[i, 0] < busy_until or not np.isfinite(atr[i - 1]):
                continue
            move = (c[i] / c[i - 1] - 1) * 100
            med = np.median(v[i - 48:i])
            if abs(move) > 3 * atr[i - 1] and med > 0 and v[i] > 3 * med:
                side = -1 if move > 0 else 1
                row = {'period': period, 'pair': pair, 't': a1[i, 0], 'side': side}
                for h in HORIZONS:
                    row[f'f{h}'] = forward(c, i, h) * side
                out.append(row)
                busy_until = a1[i, 0] + 72 * H
    return out


def momentum_days(period, k=3):
    cache = D.PERIODS[period]
    closes = {}
    for pair in D.PAIRS:
        a1 = D.load(cache, pair, '1h')
        if a1 is not None:
            closes[pair] = pd.Series(a1[:, 4], index=a1[:, 0].astype('int64'))
    frame = pd.DataFrame(closes).sort_index()
    daily = frame[(frame.index // H) % 24 == 23]     # закрытие часа 23:00 → решение в 00:00
    rows = []
    for j in range(1, len(daily) - 1):
        past = daily.iloc[j] / daily.iloc[j - 1] - 1
        ahead = daily.iloc[j + 1] / daily.iloc[j] - 1
        ok = past.notna() & ahead.notna()
        if ok.sum() < 2 * k + 2:
            continue
        order = past[ok].sort_values().index
        spread = (ahead[order[-k:]].mean() - ahead[order[:k]].mean()) * 100
        rows.append({'period': period, 't': int(daily.index[j]), 'spread': spread})
    return rows


def report(name, rows, cost):
    frame = pd.DataFrame(rows)
    if frame.empty:
        print(f'\n{name}: событий нет')
        return
    frame['fold'] = frame['period'].map({p: f for f, ps in FOLDS.items() for p in ps})
    frame = frame.drop_duplicates(subset=[c for c in ('pair', 't', 'side') if c in frame])
    print(f'\n{name}: {len(frame)} событий; издержки круга {cost}%')
    cols = [c for c in frame.columns if c.startswith('f') and c[1:].isdigit()] or ['spread']
    for col in cols:
        cells = []
        for fold in FOLDS:
            part = frame[frame.fold == fold][col].dropna()
            cells.append(f'{fold} {part.mean():+.3f}% ({len(part)})')
        allv = frame[col].dropna().to_numpy()
        lo, hi = ci(allv)
        net = allv.mean() - cost
        print(f'   {col:>6s}: ' + ', '.join(cells) + f'  | все {allv.mean():+.3f}% [{lo:+.3f}; {hi:+.3f}], '
              f'за вычетом издержек {net:+.3f}%')


def main():
    fund, casc, mom = [], [], []
    for period in D.PERIODS:
        fund += funding_events(period)
        casc += cascade_events(period)
        mom += momentum_days(period)
    report('1. КРАЙНИЙ ФАНДИНГ ПРОТИВ ТОЛПЫ', fund, 0.11)
    report('2. ВЫНОС С ЛИКВИДАЦИЯМИ (свеча > 3 ATR на объёме > 3 медиан), против хода', casc, 0.11)
    report('3. СУТОЧНЫЙ ИМПУЛЬС: верхние 3 минус нижние 3, ход следующих суток', mom, 0.15)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
