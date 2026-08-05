"""
Круг 16: ненаполнившийся лимит — самая большая потеря во всей цепочке.

Портфельная воронка (research/smc_capacity.py):

    бычий:    1505 ордеров -> 396 сделок (26%)
        лимит не налился              639   42%
        лимит позиций в одну сторону  321   21%
        по паре уже есть позиция       83    6%
        кулдаун                        42    3%
        нет свободного слота           23    2%

    медвежий: 1528 ордеров -> 436 сделок (29%)
        лимит не налился              758   50%

Ненаполнившийся вход теряет больше, чем все остальные ограничения вместе.
Слоты и кулдаун не ограничивают ничего: их ослабление дало +1 сделку.
Лимит на позиции в одну сторону количество даёт (+50), но ломает медвежий
период (сумма R 44.9 -> 35.3, просадка 36.7% -> 42.5%) — снова отвергнут.

Остаётся один рычаг. Проверяются два независимых способа налить чаще:

  1. ЖИТЬ ДОЛЬШЕ. PENDING_ORDER_MAX_HOURS=48. Цена может дойти до зоны на
     третьи сутки. Геометрия сделки при этом не меняется вообще — ни вход,
     ни стоп, ни цели, ни RR. Чистый прирост количества, если он есть.

  2. НЕ ЛЕЗТЬ ТАК ГЛУБОКО. POI_ENTRY_OFFSET сдвигает лимит от края зоны
     навстречу цене: наливается чаще, но стоп дальше и RR хуже.

Про второй способ важна оговорка. В круге 10 сдвиг уже проверялся и был
отвергнут: он монотонно УМЕНЬШАЛ число сделок. Причина — взвешенный RR
падает, и порог MIN_RR=4.0 режет сетапы быстрее, чем сдвиг добавляет
наливов. Поэтому здесь сдвиг меряется В ПАРЕ со снижением порога: иначе
измеряется не сдвиг, а порог.

Приёмка прежняя и двусторонняя: сделок больше И сумма R не ниже ни на одном
периоде, просадка не выше.

Запуск:
    python research/smc_fill.py
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
    ('сейчас (48ч, без сдвига)', {}),
    # 1. Ордер живёт дольше. Геометрия сделки не меняется.
    ('жить 72ч',                 {'PENDING_ORDER_MAX_HOURS': 72.0}),
    ('жить 120ч',                {'PENDING_ORDER_MAX_HOURS': 120.0}),
    ('жить 240ч',                {'PENDING_ORDER_MAX_HOURS': 240.0}),
    # 2. Сдвиг входа — обязательно в паре с порогом, иначе меряется порог.
    ('сдвиг 15%',                {'POI_ENTRY_OFFSET': 0.15}),
    ('сдвиг 15% + RR>=3.5',      {'POI_ENTRY_OFFSET': 0.15, 'MIN_RR': 3.5}),
    ('сдвиг 30% + RR>=3.5',      {'POI_ENTRY_OFFSET': 0.30, 'MIN_RR': 3.5}),
    # 3. Лучшее из двух, если оба выживут.
    ('жить 120ч + сдвиг 15% + RR>=3.5',
     {'PENDING_ORDER_MAX_HOURS': 120.0, 'POI_ENTRY_OFFSET': 0.15, 'MIN_RR': 3.5}),
]

TRACKED = ['PENDING_ORDER_MAX_HOURS', 'POI_ENTRY_OFFSET', 'MIN_RR']

RNG = np.random.default_rng(20260805)
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
            results[(period['label'], name)] = stats
            skipped = stats.get('skipped') or {}
            print(f'   [{period["label"]}] {name}: {len(df)} сделок, '
                  f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%, '
                  f'сумма R {df.r.sum():+.1f}, не налилось '
                  f'{skipped.get("no_fill", 0)}', flush=True)
    for key, value in defaults.items():
        setattr(P, key, value)

    for period in periods:
        label = period['label']
        print()
        print('=' * 110)
        print(label.upper())
        print('=' * 110)
        head = (f'{"конфигурация":<34}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"доход/DD":>10}{"не налилось":>13}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows'].dropna(subset=['regime'])
            dd = stats['max_dd_pct']
            no_fill = (stats.get('skipped') or {}).get('no_fill', 0)
            print(f'{name:<34}{len(df):>8}{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
                  f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}'
                  f'{no_fill:>13}')

        base = results.get((label, CONFIGS[0][0]))
        if not base:
            continue
        base_r = base['rows'].dropna(subset=['regime']).r
        print()
        print('Разница с текущим (интервал через ноль = разница недоказуема):')
        for name, _ in CONFIGS[1:]:
            stats = results.get((label, name))
            if not stats:
                continue
            r = stats['rows'].dropna(subset=['regime']).r
            (lo, hi), p = diff_ci(r, base_r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<34} ΔR {r.mean() - base_r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 110)
    print('СРЕДНИЙ R ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 110)
    head = f'{"конфигурация":<34}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        frames = [results[(p['label'], name)]['rows'].dropna(subset=['regime'])
                  for p in periods if (p['label'], name) in results]
        if not frames:
            continue
        merged = pd.concat(frames, ignore_index=True)
        parts = []
        for reg in REGIMES:
            sub = merged[merged.regime == reg]
            if len(sub) < 3:
                parts.append(f'{"—":>26}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{sub.r.mean():>8.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(26))
        print(f'{name:<34}' + ''.join(parts))


if __name__ == '__main__':
    main()
