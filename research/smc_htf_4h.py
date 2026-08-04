"""
Отдельный замер: старший таймфрейм — только 4H.

Сейчас направление берётся из согласия двух рамок: тренд 1D и структура 4H
должны смотреть в одну сторону (BIAS_MODE='agree'). Вопрос стоит так: система
держит позицию часы-дни, а не недели, — насколько дневная рамка вообще
уместна в качестве фильтра, и что будет, если оставить одну 4H.

Три варианта одного и того же выбора:

    'agree'      1D и 4H согласны  (как сейчас)
    'htf_only'   только 4H         (то, что проверяется)
    'bias_only'  только 1D         (зеркальный контроль — без него нельзя
                                    отличить «4H лучше 1D» от «одна рамка
                                    лучше двух»)

Контроль обязателен: если 'bias_only' и 'htf_only' дадут одинаковый прирост,
значит дело не в выборе таймфрейма, а в снятии двойного условия.

Запуск:
    python research/smc_htf_4h.py
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
    ('1D + 4H (сейчас)', {'BIAS_MODE': 'agree'}),
    ('только 4H',        {'BIAS_MODE': 'htf_only'}),
    ('только 1D',        {'BIAS_MODE': 'bias_only'}),
    ('4H, 1D не против', {'BIAS_MODE': 'htf_unless_against'}),
]

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
    default_mode = deepcopy(P.BIAS_MODE)
    results = {}

    for name, overrides in CONFIGS:
        P.BIAS_MODE = default_mode
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
    P.BIAS_MODE = default_mode

    for period in periods:
        label = period['label']
        print()
        print('=' * 96)
        print(label.upper())
        print('=' * 96)
        head = (f'{"старший ТФ":<20}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"без целей":>11}{"3 цели":>8}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            item = results.get((label, name))
            if not item:
                continue
            df, st = item['df'], item['stats']
            print(f'{name:<20}{len(df):>8}{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{st["return_pct"]:>+9.1f}{st["max_dd_pct"]:>7.1f}'
                  f'{(df.tps == 0).mean() * 100:>10.1f}%{(df.tps == 3).mean() * 100:>7.1f}%')

        base = results.get((label, CONFIGS[0][0]))
        if not base:
            continue
        print()
        print('Разница с «1D + 4H» (интервал через ноль = разница недоказуема):')
        for name, _ in CONFIGS[1:]:
            item = results.get((label, name))
            if not item:
                continue
            (lo, hi), p = diff_ci(item['df'].r, base['df'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<20} ΔR {item["df"].r.mean() - base["df"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 96)
    print('СРЕДНИЙ R ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 96)
    head = f'{"старший ТФ":<20}' + ''.join(f'{r:>26}' for r in REGIMES)
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
        print(f'{name:<20}' + ''.join(parts))

    print()
    print('Дневная рамка снимается только если «только 4H» даёт больше сделок')
    print('и при этом не хуже по среднему R ни на одном периоде и ни в одном')
    print('режиме — иначе это размен качества на количество.')


if __name__ == '__main__':
    main()
