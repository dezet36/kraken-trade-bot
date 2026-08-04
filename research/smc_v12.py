"""
Круг 12: два лечения, найденные диагностикой падающего рынка.

Диагностика (research/smc_bear.py) дала два факта, и оба оказались НЕ
специфичными для медведя — они верны и на бычьем периоде:

  1. Первая цель стоит слишком далеко. Лестница MFE: 64% сделок доходят до
     +1R, 43% до +2R, 36% до +3R. А первая цель по фибо-расширению стоит
     около 3R — фиксирует хоть что-то лишь 30% сделок. Почти половина
     УБЫТОЧНЫХ сделок побывала в плюсе на 1R и больше (48% на быке, 45% на
     медведе) и вернулась в стоп, не зафиксировав ничего.

  2. Лонги слабее шортов во всех трёх режимах и на обоих периодах: 0.144 R
     против 0.390 R. Шесть замеров, шесть раз один знак.

Проверяются:

    hybrid 1.0R / 1.5R / 2.0R   первая цель по R вместо дальнего фибо
    премия лонгу +0.5 / +1.0    покупкам нужен более сильный сетап
    лучшее из двух вместе       если оба выживут по отдельности

Приёмка: сумма R не хуже НИ НА ОДНОМ периоде и просадка не выше. Средний R
на этих объёмах неразличим, поэтому решает пара «сумма R + просадка», а не
одна цифра.

Запуск:
    python research/smc_v12.py
"""

import os
import sys
from copy import deepcopy

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period, run)

# Ближняя цель ДОБАВЛЯЕТСЯ четвёртой ступенью, а не вытесняет дальнюю.
# Первый заход этого не учёл: _build_trade режет список целей по числу долей
# (targets = raw_targets[:len(fractions)]), и с тремя долями ближняя цель
# выкидывала самую дальнюю — ту, что даёт +6.0 R и всю прибыль. Замер тогда
# показал +50.7% против +439.8%, но проверял он не «фиксировать раньше», а
# «отрезать хвост». Четвёртая доля сохраняет хвост.
NEAR = {'TP_MODE': 'hybrid', 'TP_CLOSE_FRACTIONS': (0.20, 0.20, 0.20, 0.40)}

CONFIGS = [
    ('база', {}),
    ('цель1 = 1.0R',     dict(NEAR, TP1_R_MULTIPLE=1.0)),
    ('цель1 = 1.5R',     dict(NEAR, TP1_R_MULTIPLE=1.5)),
    ('цель1 = 2.0R',     dict(NEAR, TP1_R_MULTIPLE=2.0)),
    ('лонгу +0.5',       {'LONG_CONFLUENCE_PREMIUM': 0.5}),
    ('лонгу +1.0',       {'LONG_CONFLUENCE_PREMIUM': 1.0}),
    ('цель1 1.5R + лонгу +0.5',
     dict(NEAR, TP1_R_MULTIPLE=1.5, LONG_CONFLUENCE_PREMIUM=0.5)),
]

TRACKED = ['TP_MODE', 'TP1_R_MULTIPLE', 'LONG_CONFLUENCE_PREMIUM',
           'TP_CLOSE_FRACTIONS']

RNG = np.random.default_rng(20260804)
BOOTSTRAP = 10_000


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def main():
    from smc import params as P

    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    defaults = {key: deepcopy(getattr(P, key)) for key in TRACKED}
    results = {}

    for name, overrides in CONFIGS:
        for key, value in defaults.items():
            setattr(P, key, value)
        for key, value in overrides.items():
            setattr(P, key, value)
        for period in periods:
            stats = run(period)
            if stats is None:
                continue
            df = stats['rows'].dropna(subset=['regime'])
            results[(period['label'], name)] = {'stats': stats, 'df': df}
            print(f'   [{period["label"]}] {name}: {len(df)} сделок, '
                  f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%, '
                  f'сумма R {df.r.sum():+.1f}', flush=True)
    for key, value in defaults.items():
        setattr(P, key, value)

    for period in periods:
        label = period['label']
        print()
        print('=' * 104)
        print(label.upper())
        print('=' * 104)
        head = (f'{"конфигурация":<26}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"без целей":>11}{"весь хвост":>12}{"лонгов":>8}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            item = results.get((label, name))
            if not item:
                continue
            df, st = item['df'], item['stats']
            longs = (df.direction == 'LONG').mean() * 100
            # «Весь хвост» — доля сделок, дошедших до ПОСЛЕДНЕЙ цели своей
            # лестницы. У четырёхступенчатых конфигураций последняя — четвёртая,
            # у базовой — третья, поэтому число ступеней берётся из данных.
            last = int(df.tps.max()) if len(df) else 0
            print(f'{name:<26}{len(df):>8}{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{st["return_pct"]:>+9.1f}{st["max_dd_pct"]:>7.1f}'
                  f'{(df.tps == 0).mean() * 100:>10.1f}%'
                  f'{(df.tps == last).mean() * 100:>10.1f}%'
                  f'{longs:>7.0f}%')

        base = results.get((label, 'база'))
        if not base:
            continue
        print()
        print('Разница с базой (интервал через ноль = разница недоказуема):')
        for name, _ in CONFIGS[1:]:
            item = results.get((label, name))
            if not item:
                continue
            (lo, hi), p = diff_ci(item['df'].r, base['df'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<26} ΔR {item["df"].r.mean() - base["df"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 104)
    print('СРЕДНИЙ R ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 104)
    head = f'{"конфигурация":<26}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        merged = pd.concat([results[(p['label'], name)]['df'] for p in periods
                            if (p['label'], name) in results], ignore_index=True)
        parts = []
        for reg in REGIMES:
            sub = merged[merged.regime == reg]
            if len(sub) < 3:
                parts.append(f'{"—":>26}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{sub.r.mean():>8.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(26))
        print(f'{name:<26}' + ''.join(parts))

    print()
    print('=' * 104)
    print('ДОЛЯ УБЫТОЧНЫХ СДЕЛОК, ПОБЫВАВШИХ В ПЛЮСЕ (то, ради чего всё это)')
    print('=' * 104)
    head = f'{"конфигурация":<26}' + ''.join(f'{p["label"]:>22}' for p in periods)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        parts = []
        for period in periods:
            item = results.get((period['label'], name))
            if not item:
                parts.append(f'{"—":>22}')
                continue
            loss = item['df'][item['df'].r <= 0]
            share = (loss.mfe_r >= 1).mean() * 100 if len(loss) else float('nan')
            parts.append(f'{share:>21.0f}%')
        print(f'{name:<26}' + ''.join(parts))


if __name__ == '__main__':
    main()
