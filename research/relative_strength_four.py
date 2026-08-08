"""
Относительная сила на ЧЕТЫРЁХ периодах: свойство режима или свойство отрезка.

ЗАЧЕМ ЧЕТЫРЕ. Пробник на двух периодах показал устойчивый эффект на бычьем
(воспроизвёлся в обеих его половинах) и отсутствие на медвежьем. Объяснение
«моментум работает в росте» правдоподобно, но при одном наблюдении на режим
непроверяемо: фильтр режима, приделанный по n=1, есть подгонка по определению.

Два докачанных периода дают вторую пару точек, причём вторая бычья фаза — ДРУГОЙ
рынок, не тот, на котором всё настраивалось.

РЕЖИМ ОПРЕДЕЛЯЕТСЯ ИЗМЕРЕНИЕМ, А НЕ ПАМЯТЬЮ. Назвать периоды «бычьим» и
«медвежьим» по своим воспоминаниям о рынке значило бы решить задачу до её
постановки: тогда «моментум работает в росте» подтвердилось бы разметкой, а не
данными. Поэтому режим каждого периода считается как медианная доходность пар
за период — число, а не мнение.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА. Гипотеза «моментум — свойство роста»
принимается, только если:

    1. эффект положителен на ВСЕХ периодах с положительной медианной
       доходностью;
    2. и не положителен ни на одном периоде с отрицательной;
    3. при этом хотя бы на двух периодах интервал отделён от нуля.

Если знак пляшет внутри одной группы — это шум, и никакое объяснение режимом
его не спасёт. Отдельно проверяется контроль случайным рангом: разница,
неотличимая от случайной, разницей не является.

Запуск:
    python research/relative_strength_four.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import relative_strength as rs  # noqa: E402
from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

# Пары, торговавшиеся в середине 2023 года — те, что докачаны.
MID_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT',
    'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT', 'ARBUSDT', 'DOTUSDT',
    'XLMUSDT', 'NEARUSDT', 'UNIUSDT', 'AAVEUSDT', 'COTIUSDT', 'BICOUSDT',
    'SHIB1000USDT',
]

PERIODS = [
    ('2022-01 .. 2023-07', BEAR_CACHE, BEAR_PAIRS),
    ('2023-07 .. 2024-07', os.path.join(ROOT, 'research',
                                        'backtest_cache_mid1'), MID_PAIRS),
    ('2024-07 .. 2025-05', os.path.join(ROOT, 'research',
                                        'backtest_cache_mid2'), MID_PAIRS),
    ('2025-05 .. 2026-07', BULL_CACHE, BULL_PAIRS),
]

# Клетки, где двухпериодный пробник дал значимый результат. Новых осей не
# добавляем: перебор на четырёх периодах ради «где-нибудь да сойдётся» — это то,
# от чего четыре периода и должны защитить.
CELLS = [(12, 24), (24, 12), (24, 24), (168, 24)]


def regime(matrix):
    """
    Каков режим периода — числом, а не по памяти.

    Медианная доходность пар за весь период. Положительная — рост,
    отрицательная — падение. Разметка «на глаз» решила бы задачу до её
    постановки.
    """
    total = (matrix.iloc[-1] / matrix.iloc[0] - 1) * 100
    return float(total.median())


def main():
    loaded = []
    for label, cache, pairs in PERIODS:
        matrix = rs.load(cache, pairs, label)
        if matrix is None:
            print(f'   {label}: данных нет, пропускаем')
            continue
        loaded.append((label, matrix, regime(matrix)))

    print()
    print('=' * 104)
    print('РЕЖИМ КАЖДОГО ПЕРИОДА — медианная доходность пар за период')
    print('=' * 104)
    for label, matrix, median in loaded:
        kind = 'РОСТ' if median > 0 else 'ПАДЕНИЕ'
        print(f'  {label:<22}{median:>+9.1f}%   {kind:<9}'
              f'пар {matrix.shape[1]}, баров {matrix.shape[0]}')

    print()
    print('=' * 104)
    print('РАЗНОСТЬ «ВЕРХ МИНУС НИЗ» ПО ПЕРИОДАМ, % на сделку')
    print('=' * 104)
    head = f'{"окно → вперёд":<16}' + ''.join(f'{lab:>22}' for lab, _, _ in loaded)
    print(head)
    print('-' * len(head))

    table = {}
    for lookback, horizon in CELLS:
        cells = ''
        for label, matrix, _median in loaded:
            real = rs.forward_spread(matrix, lookback, horizon)
            fake = rs.forward_spread(matrix, lookback, horizon, shuffle=True)
            if len(real) < 30:
                cells += f'{"мало":>22}'
                table[(lookback, horizon, label)] = None
                continue
            lo, hi = ci(real)
            star = '*' if lo > 0 else ('°' if hi < 0 else ' ')
            table[(lookback, horizon, label)] = {
                'mean': float(real.mean()), 'lo': lo, 'hi': hi,
                'fake': float(fake.mean()), 'n': len(real)}
            cells += f'{f"{real.mean():+.3f}{star} n={len(real)}":>22}'
        print(f'{f"{lookback}ч → {horizon}ч":<16}{cells}')

    print()
    print('* интервал выше нуля, ° интервал ниже нуля, пробел — накрывает ноль.')

    print()
    print('=' * 104)
    print('ПРОВЕРКА ГИПОТЕЗЫ «МОМЕНТУМ — СВОЙСТВО РОСТА»')
    print('=' * 104)
    growth = [label for label, _m, median in loaded if median > 0]
    decline = [label for label, _m, median in loaded if median <= 0]
    print(f'периоды роста: {", ".join(growth) or "нет"}')
    print(f'периоды падения: {", ".join(decline) or "нет"}')
    print()
    print(f'{"окно → вперёд":<16}{"в росте":>26}{"в падении":>26}   вывод')
    print('-' * 96)
    for lookback, horizon in CELLS:
        def signs(labels):
            out = []
            for label in labels:
                cell = table.get((lookback, horizon, label))
                out.append('?' if cell is None else
                           ('+' if cell['mean'] > 0 else '−'))
            return ''.join(out)

        up, down = signs(growth), signs(decline)
        all_up = up and all(ch == '+' for ch in up)
        none_down = down and all(ch != '+' for ch in down)
        strong = sum(1 for label, _m, _r in loaded
                     if (table.get((lookback, horizon, label)) or {}).get('lo', -1) > 0)
        if all_up and none_down and strong >= 2:
            note = 'ГИПОТЕЗА ДЕРЖИТСЯ'
        elif all_up and none_down:
            note = f'знаки сходятся, но значимых периодов {strong} из 2 нужных'
        else:
            note = 'знак пляшет внутри группы — шум'
        print(f'{f"{lookback}ч → {horizon}ч":<16}{up:>26}{down:>26}   {note}')

    print()
    print('=' * 104)
    print('КОНТРОЛЬ: РАЗМЕТКА ПРОТИВ СЛУЧАЙНОГО РАНГА')
    print('=' * 104)
    print(f'{"окно → вперёд":<16}' + ''.join(f'{lab:>22}' for lab, _, _ in loaded))
    print('-' * 104)
    for lookback, horizon in CELLS:
        cells = ''
        for label, _m, _r in loaded:
            cell = table.get((lookback, horizon, label))
            if cell is None:
                cells += f'{"—":>22}'
                continue
            gap = cell['mean'] - cell['fake']
            cells += f'{f"{gap:+.3f} (случ. {cell["fake"]:+.3f})":>22}'
        print(f'{f"{lookback}ч → {horizon}ч":<16}{cells}')


if __name__ == '__main__':
    main()
