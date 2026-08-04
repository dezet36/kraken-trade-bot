"""
Круг 7: цели на пулах ликвидности вместо расширений сетки.

Основание — §14.2 методички («тейк-профиты располагайте на очевидных пулах
ликвидности») и разбор 471 сделки: сделка в среднем проходит 2.7R в нашу
сторону, но 68-70% не фиксируют ни одной цели. Расширения Фибоначчи
отсчитываются ЗА конец импульса и часто попадают туда, где ликвидности нет
вовсе — цене незачем туда идти. Пул ликвидности такое обоснование имеет: за
очевидным хаем или лоем стоят стопы, и рынок к ним стремится.

MIN_RR ПЕРЕБИРАЕТСЯ ВМЕСТЕ С РЕЖИМОМ ЦЕЛЕЙ. Это не косметика: порог 4.0
откалиброван под далёкие цели сетки. Пулы ликвидности лежат ближе, взвешенный
RR падает, и старый гейт зарежет сетапы ещё до того, как режим целей себя
покажет. Круг 6 наступил ровно на эти грабли: hybrid 1.0R потерял 40% сделок
не потому, что цель плоха, а потому что её не пропустил гейт.

Запуск:
    python research/smc_v7.py
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

CONFIGS = [
    ('сейчас: сетка, RR>=4',   {}),
    # Тот же гейт, другой источник целей — изолирует эффект режима
    ('ликвидность, RR>=4',     {'TP_MODE': 'liquidity'}),
    # Гейт отпускаем: цели ближе, взвешенный RR ниже по построению
    ('ликвидность, RR>=3',     {'TP_MODE': 'liquidity', 'MIN_RR': 3.0}),
    ('ликвидность, RR>=2',     {'TP_MODE': 'liquidity', 'MIN_RR': 2.0}),
    ('ликвидность, RR>=1.5',   {'TP_MODE': 'liquidity', 'MIN_RR': 1.5}),
    # Только значимые уровни: неделя/месяц/равные вершины, без мелких свингов
    ('ликвидн. значимые, RR>=2',
     {'TP_MODE': 'liquidity', 'MIN_RR': 2.0, 'LIQ_MIN_WEIGHT': 0.7}),
    # Первая цель не ближе 1.5R — проверка, не слишком ли мелко фиксируем
    ('ликвидн. от 1.5R, RR>=2',
     {'TP_MODE': 'liquidity', 'MIN_RR': 2.0, 'LIQ_MIN_R': 1.5}),
    # Для сверки: сетка с тем же отпущенным гейтом. Если выиграет она,
    # значит дело было в гейте, а не в целях.
    ('сетка, RR>=2',           {'MIN_RR': 2.0}),
]

TRACKED = ['TP_MODE', 'MIN_RR', 'LIQ_MIN_R', 'LIQ_MIN_WEIGHT', 'LIQ_MERGE_PCT',
           'TP_CLOSE_FRACTIONS', 'MIN_CONFLUENCE_SCORE', 'MAX_SAME_DIRECTION']

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
                print(f'   [{period["label"]}] {name}: сделок нет', flush=True)
                continue
            df = stats['rows'].dropna(subset=['regime'])
            results[(period['label'], name)] = {'stats': stats, 'df': df}
            print(f'   [{period["label"]}] {name}: {len(df)} сделок, '
                  f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%, '
                  f'без целей {(df.tps == 0).mean() * 100:.0f}%', flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 110)
        print(label.upper())
        print('=' * 110)
        head = (f'{"конфигурация":<26}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"винрейт":>9}{"без целей":>11}{"все цели":>10}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            item = results.get((label, name))
            if not item:
                continue
            df, st = item['df'], item['stats']
            print(f'{name:<26}{len(df):>8}{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{st["return_pct"]:>+9.1f}{st["max_dd_pct"]:>7.1f}'
                  f'{(df.r > 0).mean() * 100:>8.1f}%'
                  f'{(df.tps == 0).mean() * 100:>10.1f}%{(df.tps == 3).mean() * 100:>9.1f}%')

        base = results.get((label, CONFIGS[0][0]))
        if not base:
            continue
        print()
        print('Разница со «сейчас» (интервал через ноль = разница недоказуема):')
        for name, _ in CONFIGS[1:]:
            item = results.get((label, name))
            if not item:
                continue
            (lo, hi), p = diff_ci(item['df'].r, base['df'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<26} ΔR {item["df"].r.mean() - base["df"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 110)
    print('УСТОЙЧИВОСТЬ ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 110)
    head = f'{"конфигурация":<26}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        parts = []
        chunks = [results[(p['label'], name)]['df'] for p in periods
                  if (p['label'], name) in results]
        if not chunks:
            continue
        merged = pd.concat(chunks, ignore_index=True)
        for reg in REGIMES:
            sub = merged[merged.regime == reg]
            if len(sub) < 5:
                parts.append(f'{"—":>26}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{sub.r.mean():>8.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(26))
        print(f'{name:<26}' + ''.join(parts))

    print()
    print('Принимается только конфигурация, положительная на ВСЕХ режимах и не')
    print('уступающая текущей по просадке. Разница в доходе без разницы в')
    print('среднем R — эффект числа сделок, а не качества, и основанием не является.')


if __name__ == '__main__':
    main()
