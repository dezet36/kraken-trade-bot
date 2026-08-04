"""
Диагностика падающего рынка: ПОЧЕМУ на медведе 0.103 R против 0.469 R на быке.

Замер, а не догадка. Прежде чем что-то адаптировать, нужно знать, что именно
ломается. Пять версий, каждая проверяема на этих данных:

    1. «Лонги против тренда» — на падающем рынке покупки убыточны, шорты нет.
       Проверка: разбивка по направлению внутри периода.

    2. «Не доходит до целей» — движения короче, TP3 недостижим.
       Проверка: доля сделок без единой цели и распределение MFE.

    3. «Отдаёт прибыль» — сделка уходит в плюс на 1-2R и возвращается в минус.
       Проверка: доля убыточных сделок, у которых MFE был >= 1R.

    4. «Лимит направления мешает» — MAX_SAME_DIRECTION=3 режет шорты именно
       тогда, когда они правы. На падающем рынке коррелированность — не риск,
       а сам источник прибыли.
       Проверка: сколько сетапов теряется на этом лимите в каждом периоде.

    5. «Всё решили пара месяцев» — период не однороден, минус собран в одном
       окне. Проверка: помесячная сумма R.

Никаких параметров здесь не меняется — это чистое измерение базовой
конфигурации.

Запуск:
    python research/smc_bear.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period, run)

pd.set_option('display.width', 200)


def block(title):
    print()
    print('=' * 92)
    print(title)
    print('=' * 92)


def by_direction(frames):
    block('1. НАПРАВЛЕНИЕ: убивают ли лонги на падающем рынке')
    head = (f'{"период":<18}{"сторона":<9}{"сделок":>8}{"винрейт":>9}'
            f'{"R/сделку":>10}{"сумма R":>9}{"интервал среднего":>24}')
    print(head)
    print('-' * len(head))
    for label, df in frames.items():
        for side in ('LONG', 'SHORT'):
            sub = df[df.direction == side]
            if len(sub) < 3:
                continue
            lo, hi = ci(sub.r)
            print(f'{label:<18}{side:<9}{len(sub):>8}{(sub.r > 0).mean() * 100:>8.0f}%'
                  f'{sub.r.mean():>10.3f}{sub.r.sum():>9.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}')


def by_direction_regime(frames):
    block('1б. НАПРАВЛЕНИЕ ВНУТРИ РЕЖИМА (оба периода вместе)')
    merged = pd.concat(frames.values(), ignore_index=True)
    head = f'{"режим":<12}{"сторона":<9}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}{"интервал":>24}'
    print(head)
    print('-' * len(head))
    for reg in REGIMES:
        for side in ('LONG', 'SHORT'):
            sub = merged[(merged.regime == reg) & (merged.direction == side)]
            if len(sub) < 3:
                continue
            lo, hi = ci(sub.r)
            print(f'{reg:<12}{side:<9}{len(sub):>8}{sub.r.mean():>10.3f}'
                  f'{sub.r.sum():>9.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>24}')


def by_reach(frames):
    block('2-3. ДОХОДЯТ ЛИ ДО ЦЕЛЕЙ И СКОЛЬКО ОТДАЮТ ОБРАТНО')
    head = (f'{"период":<18}{"без целей":>11}{"3 цели":>9}{"MFE медиана":>13}'
            f'{"MFE>=1R":>9}{"отдали>=1R":>12}{"отдали>=2R":>12}{"дней":>7}')
    print(head)
    print('-' * len(head))
    for label, df in frames.items():
        loss = df[df.r <= 0]
        gave1 = (loss.mfe_r >= 1).mean() * 100 if len(loss) else float('nan')
        gave2 = (loss.mfe_r >= 2).mean() * 100 if len(loss) else float('nan')
        print(f'{label:<18}{(df.tps == 0).mean() * 100:>10.1f}%'
              f'{(df.tps == 3).mean() * 100:>8.1f}%{df.mfe_r.median():>13.2f}'
              f'{(df.mfe_r >= 1).mean() * 100:>8.0f}%{gave1:>11.0f}%{gave2:>11.0f}%'
              f'{df.days.median():>7.1f}')
    print()
    print('«отдали>=1R» — доля УБЫТОЧНЫХ сделок, которые побывали в плюсе на 1R и')
    print('больше. Высокая доля означает, что проблема не во входах, а в выходах.')


def mfe_ladder(frames):
    block('2б. КАК ДАЛЕКО ВООБЩЕ УХОДИТ ЦЕНА В НАШУ СТОРОНУ')
    steps = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
    head = f'{"период":<18}' + ''.join(f'{f">={s}R":>9}' for s in steps)
    print(head)
    print('-' * len(head))
    for label, df in frames.items():
        print(f'{label:<18}' + ''.join(f'{(df.mfe_r >= s).mean() * 100:>8.0f}%'
                                       for s in steps))


def by_month(frames):
    block('5. ПОМЕСЯЧНО: собран ли минус в одном окне')
    for label, df in frames.items():
        print()
        print(label)
        month = df.set_index('entry_time').resample('MS').r.agg(['count', 'sum', 'mean'])
        month = month[month['count'] > 0]
        for ts, row in month.iterrows():
            bar = ('+' if row['sum'] >= 0 else '-') * min(int(abs(row['sum'])), 60)
            print(f'   {ts:%Y-%m}  {int(row["count"]):>4} сд  '
                  f'{row["sum"]:>+7.1f} R  {row["mean"]:>+6.2f} ср  {bar}')
        pos = (month['sum'] > 0).mean() * 100
        print(f'   прибыльных месяцев: {pos:.0f}% из {len(month)}')


def by_exit(frames):
    block('ЧЕМ ЗАКАНЧИВАЮТСЯ СДЕЛКИ')
    for label, df in frames.items():
        print()
        print(label)
        grouped = df.groupby('reason').r.agg(['count', 'sum', 'mean'])
        grouped = grouped.sort_values('count', ascending=False)
        for reason, row in grouped.iterrows():
            print(f'   {reason:<16}{int(row["count"]):>5} сд '
                  f'{row["count"] / len(df) * 100:>5.0f}%  '
                  f'{row["sum"]:>+7.1f} R  {row["mean"]:>+6.2f} ср')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    frames = {}
    for period in periods:
        stats = run(period)
        if stats is None:
            continue
        df = stats['rows'].dropna(subset=['regime']).copy()
        df['entry_time'] = pd.to_datetime(df['entry_time'])
        frames[period['label']] = df
        print(f'   [{period["label"]}] {len(df)} сделок, '
              f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%', flush=True)

    by_direction(frames)
    by_direction_regime(frames)
    by_reach(frames)
    mfe_ladder(frames)
    by_exit(frames)
    by_month(frames)

    block('ЧТО ИЗ ЭТОГО СЛЕДУЕТ')
    print('Выводы делаются по таблицам выше, а не заранее. Правило прежнее:')
    print('интервал среднего, пересекающий ноль, ничего не доказывает — но')
    print('одинаковое направление эффекта на двух независимых периодах уже')
    print('является доводом, а просадка и доля «отдали обратно» — это свойства')
    print('траектории, они устойчивее среднего R.')


if __name__ == '__main__':
    main()
