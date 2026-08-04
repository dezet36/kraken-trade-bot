"""
Круг 6: близкая первая цель.

Разбор по режимам (research/smc_market_regime.py) снял прежнюю гипотезу:
стратегия НЕ теряет в боковике. Средний R по режимам 0.431 / 0.345 / 0.446,
интервалы перекрываются — режим рынка вообще не объясняет результат.

Зато нашлось другое. Сделка в среднем проходит 2.7R в нашу сторону, но
68-70% сделок не фиксируют НИ ОДНОЙ цели и умирают чистым стопом. Цели стоят
на расширениях сетки Фибоначчи, то есть ЗА концом импульса, а вход — глубоко
в коррекции: чтобы дать первый тейк, цене нужно пройти всю ногу обратно и
выйти за неё. Движение на 2R происходит, фиксировать его нечем.

Проверяем режим TP_MODE='hybrid': первая цель на фиксированном кратном риска,
остальные по сетке. Механизм в коде уже есть и никогда не включался на
нынешней конфигурации (он писался при других весах confluence и пороге 4.5).

Дополнительно проверяется BIAS_REQUIRE_CONFIRMED — структурный фильтр режима,
тоже ни разу не принятый.

Оценка честная: результат меряется на обоих периодах, разница со «сейчас»
проверяется бутстрэпом. Разница в итоговом проценте без разницы в среднем R —
это эффект числа сделок, а не качества, и основанием для решения не является.

Запуск:
    python research/smc_v6.py
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
    ('сейчас (fib)',        {}),
    ('hybrid 1.0R',         {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.0}),
    ('hybrid 1.5R',         {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.5}),
    ('hybrid 2.0R',         {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 2.0}),
    # Больше веса на раннюю фиксацию: если до дальних целей доходит каждая
    # пятая сделка, стоит ли держать под них половину позиции
    ('hybrid 1.5R + 40/30/30', {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.5,
                                'TP_CLOSE_FRACTIONS': (0.40, 0.30, 0.30)}),
    ('подтверждённый тренд', {'BIAS_REQUIRE_CONFIRMED': True}),
]

TRACKED = ['TP_MODE', 'TP1_R_MULTIPLE', 'TP_CLOSE_FRACTIONS',
           'BIAS_REQUIRE_CONFIRMED', 'MIN_CONFLUENCE_SCORE', 'MAX_SAME_DIRECTION']

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
