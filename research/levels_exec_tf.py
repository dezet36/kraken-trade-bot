"""
Уровни: исполнение на часе против пятиминуток. Одно изменение, четыре периода.

ОТКУДА ВОПРОС. Уровни — единственная из четырёх стратегий, которая и ищет
сигнал, и исполняется на часовых свечах. У Фибоначчи и SMC сигнал часовой, а
исполнение пятиминутное.

Внутрисвечная неоднозначность решается движком консервативно: если в одной
свече задеты и стоп, и цель, считается сработавшим СТОП. Обратное допущение
систематически завышало бы результат. Но на часовой свече такая
неоднозначность возникает заметно чаще, чем на пятиминутной, — значит
измеренный результат уровней скорее занижен, чем завышен, и величина занижения
неизвестна.

ЧТО ЗДЕСЬ МЕНЯЕТСЯ. Ровно одно: свечи исполнения. Сигналы, пороги, геометрия,
портфельные настройки — всё то же, заявки те же самые. Поэтому сравнение
ПАРНОЕ, по совпадающим ключам: одна и та же заявка проходит через два набора
свечей, и разница относится к исполнению, а не к разнице в наборе сделок.

Непарное сравнение здесь ничего не показало бы: у уровней около 450 сделок за
период, интервал средней ±0.17 R, а интервал разницы двух независимых выборок
был бы около ±0.24 — то есть требовал бы улучшения крупнее самого края.

ЧЕГО ЖДУ ЗАРАНЕЕ, ЧТОБЫ ПОТОМ НЕ ОБЪЯСНЯТЬ ЗАДНИМ ЧИСЛОМ. Пятиминутное
исполнение должно дать НЕ ХУЖЕ часового: часть сделок, помеченных стопом из-за
неоднозначности, окажется целями. Если разница выйдет отрицательной, значит
предположение о занижении неверно, и это тоже ответ — но неожиданный, и тогда
надо будет искать причину, а не объявлять пятиминутки вредными.

ЧЕТЫРЕ ПЕРИОДА, А НЕ ДВА. Оба прежних проверочных периода — падающие (у
«бычьего» BTC −39.8%). Улучшение исполнения не должно зависеть от режима, и
если оно зависит — это признак того, что мерится не то, что думали.

Запуск:
    python research/levels_exec_tf.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import direction_across as da  # noqa: E402
from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from common import RISING_CACHES, RISING_PAIRS, ci, hush, unhush  # noqa: E402

PERIODS = [
    ('2022-01 падение', BEAR_CACHE, BEAR_PAIRS),
    ('2023-07 РОСТ',    RISING_CACHES[0], RISING_PAIRS),
    ('2024-07 РОСТ',    RISING_CACHES[1], RISING_PAIRS),
    ('2025-05 падение', BULL_CACHE, BULL_PAIRS),
]


def run(orders, exec_data):
    from levels import params as LP
    from smc_engine import compute_stats, run_portfolio

    result = run_portfolio(
        orders, exec_data, risk_pct=LP.RISK_PCT,
        max_positions=LP.MAX_POSITIONS, cooldown_hours=LP.COOLDOWN_HOURS,
        max_same_direction=LP.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False, max_hold_hours=LP.MAX_HOLD_HOURS)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 20:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    reasons = {}
    for trade in trades:
        key = trade.get('exit_reason', '?').split('_')[0]
        reasons[key] = reasons.get(key, 0) + 1
    return {'r': r, 'n': len(trades), 'mean': float(r.mean()),
            'wr': float((r > 0).mean() * 100), 'total': float(r.sum()),
            'dd': stats['max_dd_pct'], 'reasons': reasons,
            'by_key': {t['key']: t['pnl'] / t['risk'] for t in trades}}


def paired(base, other):
    """Разница по СОВПАДАЮЩИМ заявкам: вклад исполнения, а не набора сделок."""
    keys = set(base['by_key']) & set(other['by_key'])
    if len(keys) < 20:
        return None
    diff = np.array([other['by_key'][k] - base['by_key'][k] for k in keys],
                    dtype=float)
    lo, hi = ci(diff)
    return {'n': len(keys), 'mean': float(diff.mean()), 'lo': lo, 'hi': hi,
            'moved': int(np.sum(np.abs(diff) > 1e-9))}


def main():
    rows = {}
    for label, cache, pairs in PERIODS:
        data = da.load(cache, pairs, label)
        if not data:
            print(f'   {label}: данных нет')
            continue
        pairs_here = list(data)
        missing = [p for p in pairs_here if '5m' not in data[p]]
        if missing:
            print(f'   {label}: без пятиминуток — {len(missing)} пар, пропускаю их')
        usable = [p for p in pairs_here if '5m' in data[p]]
        quiet = hush()
        try:
            orders = da.orders_levels(data, usable)
        finally:
            unhush(quiet)
        print(f'   {label}: заявок {len(orders)}', flush=True)
        if len(orders) < 20:
            continue
        on_1h = run(orders, {p: data[p]['1h'] for p in usable})
        on_5m = run(orders, {p: data[p]['5m'] for p in usable})
        rows[label] = (on_1h, on_5m, paired(on_1h, on_5m) if on_1h and on_5m
                       else None)

    print()
    print('=' * 112)
    print('УРОВНИ: ИСПОЛНЕНИЕ НА ЧАСЕ ПРОТИВ ПЯТИМИНУТОК')
    print('=' * 112)
    head = (f'{"период":<20}{"сделок":>8}{"винрейт 1ч":>12}{"винрейт 5м":>12}'
            f'{"R 1ч":>9}{"R 5м":>9}{"парная разница":>18}'
            f'{"интервал":>22}')
    print(head)
    print('-' * len(head))
    for label, (on_1h, on_5m, diff) in rows.items():
        if not on_1h or not on_5m:
            print(f'{label:<20}{"— мало сделок":>16}')
            continue
        gap = f'{diff["mean"]:+.3f}' if diff else '—'
        span = f'[{diff["lo"]:+.3f}; {diff["hi"]:+.3f}]' if diff else '—'
        print(f'{label:<20}{on_1h["n"]:>8}{on_1h["wr"]:>11.1f}%'
              f'{on_5m["wr"]:>11.1f}%{on_1h["mean"]:>9.3f}{on_5m["mean"]:>9.3f}'
              f'{gap:>18}{span:>22}')

    print()
    print('=' * 112)
    print('ЧЕМ КОНЧАЛИСЬ СДЕЛКИ — где именно неоднозначность решалась иначе')
    print('=' * 112)
    print(f'{"период":<20}{"на часе":>34}{"на пятиминутках":>34}')
    print('-' * 90)
    for label, (on_1h, on_5m, _d) in rows.items():
        if not on_1h or not on_5m:
            continue

        def show(res):
            return ', '.join(f'{k} {v}' for k, v in
                             sorted(res['reasons'].items(), key=lambda x: -x[1]))

        print(f'{label:<20}{show(on_1h):>34}{show(on_5m):>34}')

    print()
    print('=' * 112)
    print('ВЫВОД')
    print('=' * 112)
    signs = [d['mean'] for _l, (_a, _b, d) in rows.items() if d]
    strong = sum(1 for _l, (_a, _b, d) in rows.items() if d and d['lo'] > 0)
    moved = [d['moved'] / d['n'] * 100 for _l, (_a, _b, d) in rows.items() if d]
    if not signs:
        print('сравнивать нечего')
    else:
        print(f'разница положительна на {sum(1 for s in signs if s > 0)} '
              f'периодах из {len(signs)}, значима на {strong}')
        print(f'доля сделок, у которых результат изменился: '
              f'{np.mean(moved):.0f}% в среднем')
        print()
        print('Ожидание, записанное ДО прогона: пятиминутки не хуже часа, потому')
        print('что часть сделок, помеченных стопом из-за внутрисвечной')
        print('неоднозначности, окажется целями. Отрицательная разница означала')
        print('бы, что предположение о занижении неверно, — и тогда надо искать')
        print('причину, а не объявлять пятиминутки вредными.')


if __name__ == '__main__':
    main()
