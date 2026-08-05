"""
Куда девается 74% сетапов: разбор портфельных ограничений.

Сигнал рождает 1505 ордеров на бычьем периоде, сделками становятся 396.
Все круги 11-14 крутили параметры СИГНАЛА и не приняли ни одного изменения.
Но между сигналом и сделкой стоит вторая воронка — портфельная, и её
пороги выбирались тогда же, на ядре с дефектом merge_swings:

    MAX_POSITIONS      сколько позиций держим одновременно
    COOLDOWN_HOURS     пауза по паре после выхода
    MAX_SAME_DIRECTION лимит позиций в одну сторону
    лимитный вход      ордер, который не налился, — это несостоявшаяся сделка

Здесь сначала печатается сама воронка (сколько ордеров умирает на каждом
ограничении), потом меряется ослабление каждого по отдельности.

Приёмка прежняя и двусторонняя: сделок больше И сумма R не ниже ни на одном
периоде, просадка не выше.

Запуск:
    python research/smc_capacity.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period)

RNG = np.random.default_rng(20260804)
BOOTSTRAP = 10_000

REASON_RU = {
    'duplicate': 'та же зона уже отработана',
    'active': 'по паре уже есть позиция',
    'cooldown': 'кулдаун по паре',
    'capacity': 'нет свободного слота',
    'same_direction': 'лимит позиций в одну сторону',
    'no_fill': 'лимитный вход не налился',
    'risk_zero': 'риск обнулён режимом',
}

# имя -> переопределения (slots, cooldown, same_dir)
CONFIGS = [
    ('сейчас',            {}),
    ('слотов 7',          {'slots': 7}),
    ('слотов 10',         {'slots': 10}),
    ('кулдаун 6ч',        {'cooldown': 6.0}),
    ('кулдаун 0',         {'cooldown': 0.0}),
    ('в одну сторону 4',  {'same_dir': 4}),
    ('в одну сторону 5',  {'same_dir': 5}),
    ('слотов 7 + кулдаун 6ч', {'slots': 7, 'cooldown': 6.0}),
]


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def build_orders_for(period):
    from smc_sweep import build_orders
    orders = []
    for pair in period['data']:
        orders += build_orders(period['contexts'][pair], pair,
                               period['data'][pair]['1h'])
    return orders


def run_with(period, orders, slots=None, cooldown=None, same_dir=None):
    from smc import params as P
    from smc_engine import compute_stats, run_portfolio
    bt = period['bt']
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=bt.RISK_PCT,
        max_positions=bt.MAX_POSITIONS if slots is None else slots,
        cooldown_hours=bt.COOLDOWN_HOURS if cooldown is None else cooldown,
        max_same_direction=(P.MAX_SAME_DIRECTION if same_dir is None else same_dir))
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        rows.append({'r': t['pnl'] / t['risk'],
                     'regime': period['regime'](t['entry_time'])})
    stats['rows'] = pd.DataFrame(rows)
    stats['skipped_raw'] = result['skipped']
    return stats


def funnel(label, orders, stats):
    print()
    print(f'{label}: ордеров {len(orders)} -> сделок {len(stats["rows"])} '
          f'({len(stats["rows"]) / len(orders):.0%})')
    lost = stats['skipped_raw']
    total = sum(lost.values()) or 1
    for key, count in sorted(lost.items(), key=lambda kv: -kv[1]):
        if not count:
            continue
        name = REASON_RU.get(key, key)
        bar = '█' * int(round(count / total * 40))
        print(f'   {name:<32}{count:>5}  {count / len(orders):>4.0%}  {bar}')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    orders = {p['label']: build_orders_for(p) for p in periods}

    print()
    print('=' * 96)
    print('ПОРТФЕЛЬНАЯ ВОРОНКА ПРИ ТЕКУЩИХ ОГРАНИЧЕНИЯХ')
    print('=' * 96)
    results = {}
    for period in periods:
        stats = run_with(period, orders[period['label']])
        results[(period['label'], 'сейчас')] = stats
        funnel(period['label'], orders[period['label']], stats)

    for name, over in CONFIGS[1:]:
        for period in periods:
            stats = run_with(period, orders[period['label']], **over)
            if stats is None:
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: {len(df)} сделок, '
                  f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%, '
                  f'сумма R {df.r.sum():+.1f}', flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 100)
        print(label.upper())
        print('=' * 100)
        head = (f'{"конфигурация":<24}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"доход/DD":>10}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            dd = stats['max_dd_pct']
            print(f'{name:<24}{len(df):>8}{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
                  f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}')

        base = results.get((label, 'сейчас'))
        if not base:
            continue
        print()
        print('Разница с текущим (интервал через ноль = разница недоказуема):')
        for name, _ in CONFIGS[1:]:
            stats = results.get((label, name))
            if not stats:
                continue
            (lo, hi), p = diff_ci(stats['rows'].r, base['rows'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<24} ΔR {stats["rows"].r.mean() - base["rows"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 100)
    print('СРЕДНИЙ R ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 100)
    head = f'{"конфигурация":<24}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        frames = [results[(p['label'], name)]['rows'] for p in periods
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
        print(f'{name:<24}' + ''.join(parts))


if __name__ == '__main__':
    main()
