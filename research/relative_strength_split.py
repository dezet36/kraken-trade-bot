"""
Относительная сила: свойство режима или свойство периода?

ОТКУДА ВОПРОС. Пробник показал, что ранг предсказывает будущую разность пар на
БЫЧЬЕМ периоде (четыре клетки с интервалом, отделённым от нуля, на разных
окнах) и НЕ предсказывает на медвежьем, где короткие окна значимо отрицательны:
слабейшие отскакивают сильнее.

Соблазн объяснить это режимом рынка и навесить фильтр «торгуем моментум только
в росте» очень велик. Но это ровно то, как выглядит подгонка: «работает, когда
работает». По одному наблюдению на режим такое утверждение непроверяемо.

ЧТО РАЗЛИЧАЕТ ОДНО ОТ ДРУГОГО. Разрезаем КАЖДЫЙ период пополам по времени и
смотрим на четыре подвыборки:

    свойство режима — обе половины быка положительны И обе половины медведя
                      отрицательны. Знак определяется рынком, а не отрезком;
    свойство шума   — половины одного и того же периода расходятся между
                      собой. Тогда исходный результат был случайным.

Это тот же приём, что и с отложенными парами у Боллинджера, только режущий по
времени: разрезав период, мы получаем два отрезка ОДНОГО рынка, и если знак на
них разный, объяснять его режимом нельзя.

ЧЕСТНО ПРО МОЩНОСТЬ. Половина периода — половина наблюдений, интервалы станут
заметно шире. Поэтому смотрим прежде всего на СОГЛАСОВАННОСТЬ ЗНАКА, а не на
значимость каждой половины по отдельности: четыре совпадения подряд сами по
себе маловероятны при отсутствии эффекта.

Запуск:
    python research/relative_strength_split.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import relative_strength as rs  # noqa: E402
from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

# Клетки, ради которых всё затевалось: те, где пробник дал значимый результат,
# плюс короткие окна, где медведь показал разворот. Берём с оглядкой на число
# наблюдений — при 130 на период половинки дадут по 65, и мерить будет нечего.
CELLS = [(12, 24), (24, 12), (24, 24), (168, 24), (4, 4), (12, 4), (72, 24)]


def halves(matrix):
    """Два непересекающихся отрезка одного периода."""
    cut = len(matrix) // 2
    return matrix.iloc[:cut], matrix.iloc[cut:]


def main():
    parts = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        matrix = rs.load(cache, pairs, label)
        if matrix is None:
            continue
        first, second = halves(matrix)
        parts[f'{label} · 1-я половина'] = first
        parts[f'{label} · 2-я половина'] = second
        print(f'      разрез: {len(first)} и {len(second)} баров', flush=True)

    names = list(parts)
    print()
    print('=' * 118)
    print('РАЗНОСТЬ «ВЕРХ МИНУС НИЗ» ПО ЧЕТЫРЁМ ПОДВЫБОРКАМ, % на сделку')
    print('=' * 118)
    head = f'{"окно → вперёд":<18}' + ''.join(f'{n:>25}' for n in names)
    print(head)
    print('-' * len(head))

    verdicts = {}
    for lookback, horizon in CELLS:
        cells, signs = '', []
        for name in names:
            values = rs.forward_spread(parts[name], lookback, horizon)
            if len(values) < 25:
                cells += f'{"мало":>25}'
                signs.append(0)
                continue
            lo, hi = ci(values)
            mean = float(values.mean())
            signs.append(1 if mean > 0 else -1)
            star = '*' if (lo > 0 or hi < 0) else ' '
            cells += f'{f"{mean:+.3f}{star} n={len(values)}":>25}'
        verdicts[(lookback, horizon)] = signs
        print(f'{f"{lookback}ч → {horizon}ч":<18}{cells}')

    print()
    print('Звёздочкой помечены клетки, где интервал не накрывает ноль.')

    print()
    print('=' * 118)
    print('ВЕРДИКТ ПО КАЖДОЙ КЛЕТКЕ')
    print('=' * 118)
    print(f'{"окно → вперёд":<18}{"бык":>14}{"медведь":>14}   вывод')
    print('-' * 78)
    for (lookback, horizon), signs in verdicts.items():
        bull = signs[0:2]
        bear = signs[2:4]
        bull_same = bull[0] == bull[1] != 0
        bear_same = bear[0] == bear[1] != 0

        def show(pair):
            marks = {1: '+', -1: '−', 0: '?'}
            return marks[pair[0]] + marks[pair[1]]

        if bull_same and bear_same and bull[0] != bear[0]:
            note = 'свойство РЕЖИМА: знак устойчив внутри и разный между'
        elif bull_same and bear_same:
            note = 'знак одинаков всюду — режим ни при чём'
        else:
            note = 'ШУМ: половины одного периода спорят между собой'
        print(f'{f"{lookback}ч → {horizon}ч":<18}{show(bull):>14}'
              f'{show(bear):>14}   {note}')

    print()
    print('КАК ЧИТАТЬ. Две половины одного периода — это два отрезка ОДНОГО')
    print('рынка. Если знак на них разный, объяснять его режимом нельзя: сам')
    print('режим внутри периода не менялся. Четыре совпадения подряд при')
    print('отсутствии эффекта маловероятны, и именно согласованность знака')
    print('здесь важнее значимости каждой половины в отдельности.')


if __name__ == '__main__':
    main()
