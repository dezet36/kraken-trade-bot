"""
Сглаживание кривой доходности SMC.

Измерения показали, что слабость SMC — не размер преимущества, а его форма:
0.397R на сделку против 0.137R у фибо (почти втрое лучше), но винрейт 23%
против 42% и просадка 28% против 22%. Низкий винрейт даёт длинные серии
убытков, а глубокая просадка портит сложный процент.

Значит, цель — не выжать больше R, а сделать кривую ровнее. Две гипотезы:

1. НАПРАВЛЕННЫЙ КЭП. Пять одновременных лонгов на криптопарах — не
   диверсификация, а одна ставка с умноженным риском: альткоины ходят за
   биткоином и в коррекции гибнут вместе.

2. МАЛАЯ ЧАСТИЧНАЯ ФИКСАЦИЯ. Раньше проверялась фиксация ТРЕТИ позиции на
   1R — она теряла 35R из 124.5, потому что обрезала огромных победителей.
   Но малая доля (15%) потеряет немного, а винрейт поднимет: 42% убыточных
   сделок доходят до 1R. Размен «немного R за ровную кривую» при узком месте
   в просадке может оказаться выгодным.

Запуск:
    python research/smc_smooth.py
"""

import os
import sys
from copy import deepcopy

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc import params as P, signal as smc_signal  # noqa: E402
from smc_engine import compute_stats, run_portfolio  # noqa: E402
from backtest_smc import COOLDOWN_HOURS, RISK_PCT, load_pair  # noqa: E402
from smc_pool import RANKED  # noqa: E402
from smc_sweep import build_orders  # noqa: E402

POOL = RANKED[:20]

# (имя, параметры генерации сетапов, направленный кэп)
CASES = [
    ('база (5 слотов)',           {}, 0),
    ('кэп 4 в сторону',           {}, 4),
    ('кэп 3 в сторону',           {}, 3),
    ('кэп 2 в сторону',           {}, 2),
    ('тейк 15% на 1R',            {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.0,
                                   'TP_CLOSE_FRACTIONS': (0.15, 0.25, 0.60)}, 0),
    ('тейк 15% на 1.5R',          {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.5,
                                   'TP_CLOSE_FRACTIONS': (0.15, 0.25, 0.60)}, 0),
    ('тейк 25% на 1R',            {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.0,
                                   'TP_CLOSE_FRACTIONS': (0.25, 0.25, 0.50)}, 0),
    ('кэп 3 + тейк 15% на 1R',    {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.0,
                                   'TP_CLOSE_FRACTIONS': (0.15, 0.25, 0.60)}, 3),
    ('кэп 3 + тейк 15% на 1.5R',  {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.5,
                                   'TP_CLOSE_FRACTIONS': (0.15, 0.25, 0.60)}, 3),
    ('кэп 4 + тейк 15% на 1R',    {'TP_MODE': 'hybrid', 'TP1_R_MULTIPLE': 1.0,
                                   'TP_CLOSE_FRACTIONS': (0.15, 0.25, 0.60)}, 4),
]

TRACKED = ['TP_MODE', 'TP1_R_MULTIPLE', 'TP_CLOSE_FRACTIONS']


def main():
    print(f'Загрузка {len(POOL)} пар...', flush=True)
    data = {}
    for pair in POOL:
        loaded = load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    available = [p for p in POOL if p in data]

    print('Построение контекстов...', flush=True)
    contexts = {}
    for pair in available:
        contexts[pair] = smc_signal.build_context({
            'bias': data[pair]['1d'], 'htf': data[pair]['4h'], 'poi': data[pair]['1h'],
        }, pair=pair)
    print(f'   готово: {len(contexts)}', flush=True)

    exec_data = {pair: data[pair]['5m'] for pair in available}
    any_df = data[available[0]]['1h']
    first = pd.Timestamp(any_df['timestamp'].iloc[0]).tz_convert('UTC').tz_localize(None)
    last = pd.Timestamp(any_df['timestamp'].iloc[-1]).tz_convert('UTC').tz_localize(None)
    midpoint = np.datetime64(first + (last - first) / 2)

    defaults = {key: deepcopy(getattr(P, key)) for key in TRACKED}
    order_cache = {}
    rows = []

    for name, overrides, cap in CASES:
        for key, value in defaults.items():
            setattr(P, key, value)
        for key, value in overrides.items():
            setattr(P, key, value)

        # Сетапы зависят только от параметров генерации, не от кэпа —
        # кэшируем, чтобы не пересчитывать одно и то же для разных кэпов
        cache_key = tuple(sorted(overrides.items()))
        if cache_key not in order_cache:
            orders = []
            for pair in available:
                orders += build_orders(contexts[pair], pair, data[pair]['1h'])
            order_cache[cache_key] = orders
        orders = order_cache[cache_key]

        def portfolio(subset):
            if not subset:
                return None
            return run_portfolio(
                subset, exec_data, risk_pct=RISK_PCT, max_positions=5,
                cooldown_hours=COOLDOWN_HOURS,
                breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
                max_hold_hours=P.MAX_POSITION_HOLD_HOURS,
                max_same_direction=cap)

        full = portfolio(orders)
        if not full or not full['trades']:
            print(f'   {name}: сделок нет', flush=True)
            continue
        h1 = portfolio([o for o in orders if o.created < midpoint])
        h2 = portfolio([o for o in orders if o.created >= midpoint])

        stats = compute_stats(full, label=name)
        s1 = compute_stats(h1) if h1 and h1['trades'] else None
        s2 = compute_stats(h2) if h2 and h2['trades'] else None

        rows.append({
            'name': name, 'trades': stats['trades'], 'ret': stats['return_pct'],
            'dd': stats['max_dd_pct'], 'wr': stats['winrate'],
            'pf': stats['profit_factor'], 'exp': stats['expectancy_r'],
            'sumr': stats['sum_r'],
            'ret_dd': stats['return_pct'] / max(stats['max_dd_pct'], 1e-9),
            'h1': s1['return_pct'] if s1 else 0.0,
            'h2': s2['return_pct'] if s2 else 0.0,
        })
        print(f'   {name:<26} n={stats["trades"]:4d} год={stats["return_pct"]:+8.1f}% '
              f'DD={stats["max_dd_pct"]:5.1f}% WR={stats["winrate"]:4.1f}% '
              f'PF={stats["profit_factor"]:.3f}', flush=True)

    print()
    print('=' * 106)
    print('СГЛАЖИВАНИЕ КРИВОЙ  (ключевая колонка — доход на единицу просадки)')
    print('=' * 106)
    header = (f'{"конфигурация":<28}{"сделок":>8}{"год%":>10}{"DD%":>7}{"WR%":>7}'
              f'{"PF":>8}{"R/сд":>8}{"доход/DD":>10}{"H1%":>9}{"H2%":>9}')
    print(header)
    print('-' * len(header))
    for row in sorted(rows, key=lambda r: -r['ret_dd']):
        flag = '' if row['h1'] > 0 and row['h2'] > 0 else '  (подгонка)'
        print(f'{row["name"]:<28}{row["trades"]:>8}{row["ret"]:>+10.1f}{row["dd"]:>7.1f}'
              f'{row["wr"]:>7.1f}{row["pf"]:>8.3f}{row["exp"]:>8.3f}'
              f'{row["ret_dd"]:>10.2f}{row["h1"]:>+9.1f}{row["h2"]:>+9.1f}{flag}')

    print('\nОриентир — фибо на 10 парах: +268.8%, DD 21.0%, WR 41.9%, '
          'доход/DD 12.8')


if __name__ == '__main__':
    main()
