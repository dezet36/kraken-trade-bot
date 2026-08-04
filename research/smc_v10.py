"""
Круг 10: количество сделок — лимиты, до которых цена не дошла.

Воронка (research/smc_funnel.py) показала, где теряется основная масса:
почти ПОЛОВИНА сетапов умирает, не открывшись — цена разворачивается, не
дойдя до границы зоны (42.4% на бычьем, 49.0% на медвежьем). Для сравнения:
на фильтрах качества теряется в разы меньше.

Проверяются три способа наливать чаще и их цена:

    POI_ENTRY_OFFSET — лимит выносится НАРУЖУ зоны, навстречу цене. Налив
        чаще, но вход хуже и стоп шире (стоп стоит за дальней границей зоны),
        значит RR ниже. Классический размен количества на качество.

    PENDING_ORDER_MAX_HOURS — ордер живёт дольше. Налив чаще, но сетап
        стареет: зона, до которой цена шла три дня, могла потерять смысл.

    MAX_SAME_DIRECTION — кэп забирает ещё 14-22% сетапов. Он принят за
        снижение просадки; проверяем, не слишком ли дорого.

Критерий тот же, что и всюду: разница по среднему R проверяется бутстрэпом,
просадка сравнивается напрямую. Больше сделок при худшем R — не улучшение.

Запуск:
    python research/smc_v10.py
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
    ('база (отступ 0, срок 48ч)', {}),
    # Лимит наружу зоны: доля высоты зоны
    ('отступ 0.10',              {'POI_ENTRY_OFFSET': 0.10}),
    ('отступ 0.25',              {'POI_ENTRY_OFFSET': 0.25}),
    ('отступ 0.50',              {'POI_ENTRY_OFFSET': 0.50}),
    # Дольше ждём налива
    ('срок 120ч',                {'PENDING_ORDER_MAX_HOURS': 120.0}),
    ('отступ 0.25 + срок 120ч',  {'POI_ENTRY_OFFSET': 0.25,
                                  'PENDING_ORDER_MAX_HOURS': 120.0}),
    # Кэп забирает 14-22% сетапов — не слишком ли дорого
    ('кэп 4',                    {'MAX_SAME_DIRECTION': 4}),
]

TRACKED = ['POI_ENTRY_OFFSET', 'PENDING_ORDER_MAX_HOURS',
           'MAX_SAME_DIRECTION', 'MIN_CONFLUENCE_SCORE', 'TP_MODE']

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
