"""
Распределение капитала между живыми стратегиями. Скользящая проверка.

ОТКУДА ВОПРОС. Три работающие стратегии получают одинаковый риск на сделку, а
края у них разные втрое-вшестеро: LEVELS даёт +0.374 R на сделку при просадке
6-18%, FIBO +0.056 при просадке 13-34%. Напрашивается вывод, что капитал
распределён неверно.

ПОЧЕМУ ЭТО СЧИТАЕТСЯ ИЗ СУММ, А НЕ ПЕРЕПРОГОНОМ. В этом боте у каждой стратегии
СВОЙ виртуальный счёт: она рискует процентом от него и не отнимает капитал у
соседей. Значит изменение доли — это множитель на её итог, и совокупный доход
считается точно, без повторного прогона. Перепрогон нужен был бы только ради
просадки: складывать кривые надо по времени, а сумм для этого мало. Об этом
сказано ниже отдельно.

ВЕСА БЕРУТСЯ ТОЛЬКО ИЗ ПРОШЛОГО. Правило, настроенное на том же периоде, где его
проверяют, всегда выглядит блестяще и никогда не повторяется. Здесь вес на
период N считается по периодам 1..N-1, поэтому первый период уходит на обучение
и в оценке не участвует. Проверяемых периодов остаётся ТРИ, и это мало —
сказано заранее, а не после результата.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    правило принимается, если превосходит равные доли на ВСЕХ трёх проверочных
    периодах. Двух из трёх недостаточно: при трёх точках две решки подряд
    выпадают в половине случаев.

Запуск:
    python research/sizing.py
"""

import io
import sys

# Итоги прогона живых стратегий на четырёх периодах (research/rising_check.py,
# 2026-08-08). Числа перенесены сюда РУКАМИ и потому продублированы в
# комментарии замера-источника: если они разойдутся, разойдутся заметно.
#
#   период            стратегия  R/сделку  сумма R  просадка
PERIODS = ['2022-01 падение', '2023-07 РОСТ', '2024-07 РОСТ', '2025-05 падение']
RESULTS = {
    'FIBO':   {'mean': [0.056, 0.108, 0.122, 0.134],
               'total': [149.7, 179.7, 212.7, 279.6],
               'dd': [33.9, 17.7, 12.9, 14.7],
               'trades': [2655, 1657, 1746, 2094]},
    'LEVELS': {'mean': [0.374, 0.245, 0.040, 0.279],
               'total': [148.0, 83.6, 14.2, 127.9],
               'dd': [6.0, 18.5, 14.5, 6.7],
               'trades': [396, 342, 357, 459]},
    'SMC':    {'mean': [0.117, 0.150, 0.018, 0.465],
               'total': [49.8, 48.4, 5.1, 181.2],
               'dd': [34.1, 42.1, 50.9, 22.8],
               'trades': [425, 323, 280, 390]},
}
NAMES = list(RESULTS)


def normalise(weights):
    """Доли, суммирующиеся в единицу. Отрицательные обнуляются."""
    clipped = {k: max(v, 0.0) for k, v in weights.items()}
    total = sum(clipped.values())
    if total <= 0:
        return {k: 1.0 / len(clipped) for k in clipped}
    return {k: v / total for k, v in clipped.items()}


def weights_equal(_history):
    return normalise({name: 1.0 for name in NAMES})


def weights_by_mean(history):
    """Доля пропорциональна КРАЮ НА СДЕЛКУ на прошлых периодах."""
    return normalise({name: sum(RESULTS[name]['mean'][i] for i in history)
                      / len(history) for name in NAMES})


def weights_by_total(history):
    """
    Доля пропорциональна СУММЕ R на прошлых периодах.

    Сумма — это край, умноженный на число возможностей. Разница с предыдущим
    правилом принципиальна: у стратегии уровней край вшестеро выше, но сделок
    вчетверо меньше, и по краю ей досталось бы больше половины капитала при
    вкладе в общий итог вдвое меньшем.
    """
    return normalise({name: sum(RESULTS[name]['total'][i] for i in history)
                      for name in NAMES})


def weights_by_ratio(history):
    """Доля пропорциональна сумме R, делённой на просадку: доход на риск."""
    out = {}
    for name in NAMES:
        gain = sum(RESULTS[name]['total'][i] for i in history)
        risk = max(sum(RESULTS[name]['dd'][i] for i in history) / len(history),
                   1.0)
        out[name] = gain / risk
    return normalise(out)


RULES = [('равные доли', weights_equal),
         ('по краю на сделку', weights_by_mean),
         ('по сумме R', weights_by_total),
         ('по доходу на просадку', weights_by_ratio)]


def portfolio_total(weights, period):
    """Совокупная сумма R при заданных долях."""
    return sum(weights[name] * RESULTS[name]['total'][period]
               for name in NAMES)


def main():
    print('ДОЛИ СЧИТАЮТСЯ ПО ПРОШЛЫМ ПЕРИОДАМ, ПЕРВЫЙ УХОДИТ НА ОБУЧЕНИЕ.')
    print('Проверочных периодов три.\n')

    table = {name: [] for name, _ in RULES}
    for period in range(1, len(PERIODS)):
        history = list(range(period))
        print(f'--- {PERIODS[period]} (доли по {period} прошлым) ---')
        for rule_name, rule in RULES:
            weights = rule(history)
            total = portfolio_total(weights, period)
            table[rule_name].append(total)
            shares = '  '.join(f'{n} {weights[n] * 100:.0f}%' for n in NAMES)
            print(f'  {rule_name:<24}{total:>8.1f} R    {shares}')
        print()

    print('=' * 78)
    print('СВОДКА: совокупная сумма R по проверочным периодам')
    print('=' * 78)
    head = f'{"правило":<26}' + ''.join(f'{p[:12]:>14}' for p in PERIODS[1:])
    print(head + f'{"итого":>10}')
    print('-' * (len(head) + 10))
    for rule_name, _ in RULES:
        row = table[rule_name]
        print(f'{rule_name:<26}' + ''.join(f'{v:>14.1f}' for v in row)
              + f'{sum(row):>10.1f}')

    print()
    print('=' * 78)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: правило принимается, только если')
    print('превосходит равные доли на ВСЕХ ТРЁХ проверочных периодах.')
    print('=' * 78)
    base = table['равные доли']
    winner = None
    for rule_name, _ in RULES:
        if rule_name == 'равные доли':
            continue
        row = table[rule_name]
        better = sum(1 for a, b in zip(row, base) if a > b)
        gain = (sum(row) / sum(base) - 1) * 100
        mark = 'ПРИНЯТО' if better == len(base) else f'нет ({better} из {len(base)})'
        print(f'  {rule_name:<26}лучше на {better} из {len(base)}, '
              f'итог {gain:+.1f}%   {mark}')
        if better == len(base) and (winner is None
                                    or sum(row) > sum(table[winner])):
            winner = rule_name

    print()
    if winner:
        print(f'ПРИНЯТО: {winner}.')
        print('Прибавка к совокупному итогу: '
              f'{(sum(table[winner]) / sum(base) - 1) * 100:+.1f}%.')
    else:
        print('НИ ОДНО ПРАВИЛО НЕ ПРИНЯТО — равные доли остаются.')

    print()
    print('ЧЕГО ЭТОТ ЗАМЕР НЕ ГОВОРИТ, И ЭТО ВАЖНЕЕ ПРИБАВКИ.')
    print('1. Просадку из сумм посчитать НЕЛЬЗЯ: складывать кривые надо по')
    print('   времени. Правило, отдающее долю одной стратегии, повышает')
    print('   концентрацию, и просадка может вырасти сильнее дохода.')
    print('2. Проверочных периодов ТРИ. Правило, выигравшее трижды подряд, при')
    print('   честной монете встречается в каждом восьмом случае.')
    print('3. Веса пересчитываются раз в период, то есть раз в полгода-год. На')
    print('   более частом пересчёте они начнут ловить шум.')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    main()
