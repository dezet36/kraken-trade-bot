"""
Круг 11: резерв количества — ограничения, отвергнутые на дефектном ядре.

Каждое из проверяемых здесь ограничений было принято по замерам на ядре с
дефектом merge_swings, где бэктест не видел части сломов структуры. С тех пор
они не перепроверялись, а именно они определяют, сколько сетапов вообще
рождается:

    BIAS_MODE='agree'   отсекает 72% свечей — крупнейший фильтр в системе.
        Прежний замер: ослабление до 'any' даёт +57.4% против +86.0%.
        Замер сделан на сломанном ядре.

    POI_TYPES=ORDER_BLOCK   брейкеры, митигации и зоны теней исключены как
        убыточные. Тем же замером.

    POI_MAX_TOUCHES=0   торгуются только нетронутые зоны.

    MIN_RR=4.0   взвешенный порог. Методичка требует 1:3; 4.0 выбран
        подбором — снова на сломанном ядре.

Критерий приёмки жёсткий и двусторонний: количество сделок должно вырасти
И суммарный R не должен упасть ни на одном периоде. Больше сделок при худшем
среднем R — это не рост качества, а разбавление.

Запуск:
    python research/smc_v11.py
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

# Веса из методички, расставленные по тексту ДО каких-либо замеров.
# Внешний ориентир: подогнать их под эти данные было невозможно.
CONFIGS = [
    ('база', {}),
    # Крупнейший фильтр: согласие двух старших ТФ
    ('bias: любой ТФ',        {'BIAS_MODE': 'any'}),
    ('bias: только 1D',       {'BIAS_MODE': 'bias_only'}),
    # Типы зон, исключённые прежним замером
    ('зоны +брейкер',         {'POI_TYPES_ENABLED': ('ORDER_BLOCK', 'BREAKER')}),
    ('зоны все',              {'POI_TYPES_ENABLED': ()}),
    # Тронутые зоны
    ('касаний до 1',          {'POI_MAX_TOUCHES': 1}),
    # Порог RR
    ('RR >= 3',               {'MIN_RR': 3.0}),
    ('RR >= 2.5',             {'MIN_RR': 2.5}),
]

TRACKED = ['BIAS_MODE', 'POI_TYPES_ENABLED', 'POI_MAX_TOUCHES',
           'MIN_RR', 'POI_ENTRY_OFFSET', 'MAX_SAME_DIRECTION']

RNG = np.random.default_rng(20260804)
BOOTSTRAP = 10_000


def diff_ci(a, b):
    """Интервал разности средних. Пересекает ноль — разница недоказуема."""
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
                  f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%',
                  flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 104)
        print(f'{label.upper()}')
        print('=' * 104)
        head = (f'{"конфигурация":<24}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"без целей":>11}{"3 цели":>8}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            item = results.get((label, name))
            if not item:
                continue
            df, st = item['df'], item['stats']
            print(f'{name:<24}{len(df):>8}{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{st["return_pct"]:>+9.1f}{st["max_dd_pct"]:>7.1f}'
                  f'{(df.tps == 0).mean() * 100:>10.1f}%{(df.tps == 3).mean() * 100:>7.1f}%')

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
            print(f'   {name:<24} ΔR {item["df"].r.mean() - base["df"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    # Устойчивость по режимам — то, ради чего всё затевалось
    print()
    print('=' * 104)
    print('СРЕДНИЙ R ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 104)
    head = f'{"конфигурация":<24}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        parts = []
        merged = pd.concat([results[(p['label'], name)]['df'] for p in periods
                            if (p['label'], name) in results], ignore_index=True)
        for reg in REGIMES:
            sub = merged[merged.regime == reg]
            if len(sub) < 3:
                parts.append(f'{"—":>26}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{sub.r.mean():>8.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(26))
        print(f'{name:<24}' + ''.join(parts))

    print()
    print('Принимается конфигурация, которая не хуже ни на одном режиме и лучше')
    print('хотя бы по одному доказуемому признаку (средний R или просадка).')


if __name__ == '__main__':
    main()
