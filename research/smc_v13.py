"""
Круг 13: у фильтра направления нет состояния «не знаю».

Замер режима «4H решает, 1D может запретить» дал результат, совпавший с
текущим до последней сделки: 396 и 436, те же проценты, та же просадка.
Объяснение нашлось прямым подсчётом состояний тренда:

    BTCUSDT 1D: BULLISH 52%, BEARISH 48%, NEUTRAL 0%
    BTCUSDT 4H: BULLISH 50%, BEARISH 50%, NEUTRAL 0%

NEUTRAL не встречается ни разу. При BIAS_REQUIRE_CONFIRMED=False направление
берётся из последнего события структуры, а оно всегда куда-то указывает.
Значит «1D в боковике» — состояние, которого не существует, и вся потеря
сетапов от дневной рамки приходится на честное противоречие рамок.

Отсюда вопрос, который до сих пор не проверялся: а должен ли тренд вообще
быть двоичным? §2.2 методички требует подтверждения последовательностью
HH-HL (или LH-LL). В боковике такой последовательности нет, и тренд обязан
быть NEUTRAL. Флаг BIAS_REQUIRE_CONFIRMED это включает, и он выключен.

Проверка важна вдвойне: по режимам рынка стратегия зарабатывает как раз в
боковике (+0.388 R против -0.083 в росте). Если подтверждение тренда
загоняет систему в NEUTRAL именно там, оно отрежет лучшее, что есть.
Возможен и обратный исход. Поэтому — замер.

Запуск:
    python research/smc_v13.py
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
    ('база', {}),
    ('тренд подтверждён',        {'BIAS_REQUIRE_CONFIRMED': True}),
    ('подтверждён + только 4H',  {'BIAS_REQUIRE_CONFIRMED': True,
                                  'BIAS_MODE': 'htf_only'}),
    ('подтверждён + любой ТФ',   {'BIAS_REQUIRE_CONFIRMED': True,
                                  'BIAS_MODE': 'any'}),
]

TRACKED = ['BIAS_REQUIRE_CONFIRMED', 'BIAS_MODE']

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
                  f'сумма R {df.r.sum():+.1f}', flush=True)
    for key, value in defaults.items():
        setattr(P, key, value)

    for period in periods:
        label = period['label']
        print()
        print('=' * 100)
        print(label.upper())
        print('=' * 100)
        head = (f'{"конфигурация":<26}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"без целей":>11}{"лонгов":>8}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            item = results.get((label, name))
            if not item:
                continue
            df, st = item['df'], item['stats']
            print(f'{name:<26}{len(df):>8}{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{st["return_pct"]:>+9.1f}{st["max_dd_pct"]:>7.1f}'
                  f'{(df.tps == 0).mean() * 100:>10.1f}%'
                  f'{(df.direction == "LONG").mean() * 100:>7.0f}%')

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
    print('=' * 100)
    print('СРЕДНИЙ R ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 100)
    head = f'{"конфигурация":<26}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        frames = [results[(p['label'], name)]['df'] for p in periods
                  if (p['label'], name) in results]
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
        print(f'{name:<26}' + ''.join(parts))


if __name__ == '__main__':
    main()
