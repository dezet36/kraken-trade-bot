"""
Круг 9: веса confluence — подогнанные против априорных.

Разбор факторов на исправленном ядре (research/smc_factors.py) со снятым
порогом показал: ни один фактор не имеет доказуемого влияния на обоих
периодах. Единственное исключение — structure_break на медвежьем (+1.043
[+0.54; +1.44]), но на бычьем у него нет контрпримеров, и проверить там
нечем.

Значит нынешние веса, выведенные прежним разбором, опоры в данных не имеют.
Выбирать между схемами «по результату» — снова подгонка. Поэтому сравнение
здесь служит одной цели: убедиться, что априорные веса (из методички, не
подобранные под эти данные) не катастрофически хуже. Если разница в пределах
шума, брать надо априорные — их невозможно переобучить.

Запуск:
    python research/smc_v9.py
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
BOOK_WEIGHTS = {
    'htf_bias_aligned': 1.0, 'liquidity_swept': 1.0, 'premium_discount': 1.0,
    'poi_fresh': 0.8, 'fvg_present': 0.6, 'structure_break': 0.8,
    'ote_zone': 0.5, 'killzone': 0.5, 'law_of_effort': 0.4,
}
EQUAL_WEIGHTS = {k: 1.0 for k in BOOK_WEIGHTS}
# Единственный фактор с доказуемым эффектом хоть на одном периоде
STRUCTURE_ONLY = {k: (1.0 if k in ('htf_bias_aligned', 'premium_discount',
                                    'poi_fresh', 'structure_break') else 0.0)
                  for k in BOOK_WEIGHTS}

CONFIGS = [
    ('подогнанные (сейчас)', {}),
    ('из методички',         {'CONFLUENCE_WEIGHTS': BOOK_WEIGHTS}),
    ('равные',               {'CONFLUENCE_WEIGHTS': EQUAL_WEIGHTS,
                              'MIN_CONFLUENCE_SCORE': 5.0}),
    ('только слом структуры', {'CONFLUENCE_WEIGHTS': STRUCTURE_ONLY,
                               'MIN_CONFLUENCE_SCORE': 3.8}),
    ('без порога вовсе',     {'MIN_CONFLUENCE_SCORE': 0.0}),
]

TRACKED = ['CONFLUENCE_WEIGHTS', 'MIN_CONFLUENCE_SCORE',
           'MAX_SAME_DIRECTION', 'TP_MODE']

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
