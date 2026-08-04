"""
Круг 5: отделить реальное превосходство конфигурации от везения выборки.

Круги 3 и 4 сравнивали конфигурации по итоговому проценту дохода. Так делать
опасно: доход — это сложный процент от последовательности сделок, и одна
удачно избежанная просадка в начале периода тянет за собой всю остальную
кривую. Разница «+109.6% против +82.7%» может не значить ровным счётом ничего,
если распределение результатов сделок у обеих конфигураций одинаковое.

Здесь считается то, что можно проверить на значимость:
    * средний R на сделку с доверительным интервалом (бутстрэп, 10 000 выборок);
    * суммарный R — аддитивная мера edge, без эффекта сложного процента;
    * максимальная просадка — она и есть предмет спора.

Вывод делается по пересечению интервалов: если они перекрываются, разница
между конфигурациями недоказуема, и выбирать надо по просадке, а не по доходу.

Запуск:
    python research/smc_v5.py
"""

import os
import sys
from copy import deepcopy

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_v4 import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, load_period  # noqa: E402

BOOTSTRAP = 10_000
RNG = np.random.default_rng(20260804)

CONFIGS = [
    ('без кэпа (сейчас)', {'MAX_SAME_DIRECTION': 0}),
    ('кэп 4',             {'MAX_SAME_DIRECTION': 4}),
    ('кэп 3 + conf 4.7',  {'MAX_SAME_DIRECTION': 3, 'MIN_CONFLUENCE_SCORE': 4.7}),
    ('кэп 2 + conf 4.7',  {'MAX_SAME_DIRECTION': 2, 'MIN_CONFLUENCE_SCORE': 4.7}),
]

TRACKED = ['MIN_CONFLUENCE_SCORE', 'MAX_SAME_DIRECTION']


def evaluate(period):
    """Прогон портфеля при текущих параметрах: отдаёт R по каждой сделке."""
    from smc import params as P
    from smc_sweep import build_orders
    from smc_engine import compute_stats, run_portfolio

    bt = period['bt']
    pairs = list(period['data'])
    orders = []
    for pair in pairs:
        orders += build_orders(period['contexts'][pair], pair,
                               period['data'][pair]['1h'])
    if not orders:
        return None

    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in pairs},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION)
    if not result['trades']:
        return None

    stats = compute_stats(result, label='')
    r = np.array([t['pnl'] / t['risk'] for t in result['trades'] if t['risk'] > 0])
    stats['r_multiples'] = r
    return stats


def ci(values, alpha=0.05):
    """Доверительный интервал среднего бутстрэпом — распределение R далеко не нормальное."""
    n = len(values)
    draws = RNG.choice(values, size=(BOOTSTRAP, n), replace=True).mean(axis=1)
    return np.percentile(draws, [alpha / 2 * 100, (1 - alpha / 2) * 100])


def diff_ci(a, b, alpha=0.05):
    """Интервал разности средних. Пересекает ноль -> разница недоказуема."""
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [alpha / 2 * 100, (1 - alpha / 2) * 100]), float((d > 0).mean())


def main():
    from smc import params as P

    periods = [
        ('бычий', load_period(BULL_CACHE, BULL_PAIRS, 'бычий 25-26')),
        ('медвежий', load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 22-23')),
    ]
    defaults = {key: deepcopy(getattr(P, key)) for key in TRACKED}
    results = {}

    for label, period in periods:
        for name, overrides in CONFIGS:
            for key, value in defaults.items():
                setattr(P, key, value)
            for key, value in overrides.items():
                setattr(P, key, value)
            stats = evaluate(period)
            if stats:
                results[(label, name)] = stats
                print(f'   [{label}] {name}: {stats["trades"]} сделок', flush=True)

    for label, _period in periods:
        print()
        print('=' * 96)
        print(f'{label.upper()} ПЕРИОД')
        print('=' * 96)
        head = (f'{"конфигурация":<22}{"сделок":>8}{"R/сделку":>10}'
                f'{"95% интервал":>20}{"сумма R":>10}{"доход%":>9}{"DD%":>7}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            s = results.get((label, name))
            if not s:
                continue
            r = s['r_multiples']
            lo, hi = ci(r)
            print(f'{name:<22}{len(r):>8}{r.mean():>10.3f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>20}{r.sum():>10.1f}'
                  f'{s["return_pct"]:>+9.1f}{s["max_dd_pct"]:>7.1f}')

        base = results.get((label, CONFIGS[0][0]))
        if base is None:
            continue
        print()
        print('Разница со «сейчас» (интервал через ноль = разница недоказуема):')
        for name, _ in CONFIGS[1:]:
            s = results.get((label, name))
            if not s:
                continue
            (lo, hi), p = diff_ci(s['r_multiples'], base['r_multiples'])
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<22} ΔR/сделку {s["r_multiples"].mean() - base["r_multiples"].mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('Просадка — не случайная величина одной сделки, а свойство всей кривой;')
    print('её сравниваем напрямую, без интервалов.')


if __name__ == '__main__':
    main()
