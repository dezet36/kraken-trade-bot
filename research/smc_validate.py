"""
Проверка комбинаций параметров SMC на устойчивость во времени.

Зачем отдельный скрипт: в smc_sweep.py перебирается 20 конфигураций и
выбирается лучшая. Выбор лучшего из двадцати на ОДНОМ годе — это почти
гарантированная подгонка. Здесь каждая конфигурация считается ещё и на двух
половинах периода отдельно.

Правило чтения результата: конфигурация принимается, только если она
прибыльна в ОБЕИХ половинах. Конфигурация с отличным годовым результатом,
но убыточной половиной — это подогнанный шум, а не преимущество.

Запуск:
    python research/smc_validate.py
    python research/smc_validate.py --pairs BTCUSDT,ETHUSDT,SOLUSDT
"""

import argparse
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
from backtest_smc import COOLDOWN_HOURS, MAX_POSITIONS, RISK_PCT, load_pair  # noqa: E402
from smc_sweep import BASE_PAIRS, TRACKED, build_orders  # noqa: E402

# ── Комбинации победивших факторов из первого перебора ───────────────────────
NO_BE = {'BREAKEVEN_AFTER_TP1': False}

CONFIGS = [
    # ── Базовая линия (прежний выбор, но уже на исправленном коде) ──────
    ('текущая (agree)',          {}),

    # ── Режим bias: строгое согласие 1D+4H отсеивает ~41% свечей ────────
    ('bias=any',                 {'BIAS_MODE': 'any'}),
    ('bias=1D',                  {'BIAS_MODE': 'bias_only'}),
    ('bias=4H',                  {'BIAS_MODE': 'htf_only'}),

    # ── Порог confluence при ослабленном bias ──────────────────────────
    ('bias=any+conf4.0',         {'BIAS_MODE': 'any', 'MIN_CONFLUENCE_SCORE': 4.0}),
    ('bias=any+conf3.5',         {'BIAS_MODE': 'any', 'MIN_CONFLUENCE_SCORE': 3.5}),
    ('bias=4H+conf4.0',          {'BIAS_MODE': 'htf_only', 'MIN_CONFLUENCE_SCORE': 4.0}),

    # ── Порог RR ───────────────────────────────────────────────────────
    ('bias=any+RR3',             {'BIAS_MODE': 'any', 'MIN_RR': 3.0}),
    ('bias=any+RR2',             {'BIAS_MODE': 'any', 'MIN_RR': 2.0}),
    ('bias=any+conf4.0+RR3',     {'BIAS_MODE': 'any', 'MIN_CONFLUENCE_SCORE': 4.0,
                                  'MIN_RR': 3.0}),

    # ── Типы зон: прежний вывод получен на испорченных данных ──────────
    ('только OB',                {'POI_TYPES_ENABLED': ('ORDER_BLOCK',)}),
    ('bias=any+только OB',       {'BIAS_MODE': 'any',
                                  'POI_TYPES_ENABLED': ('ORDER_BLOCK',)}),
    ('bias=any+все зоны',        {'BIAS_MODE': 'any', 'POI_TYPES_ENABLED': ()}),

    # ── Повторная проверка ключевых решений ────────────────────────────
    ('bias=any+безубыток вкл',   {'BIAS_MODE': 'any', 'BREAKEVEN_AFTER_TP1': True}),
    ('bias=any+1 касание',       {'BIAS_MODE': 'any', 'POI_MAX_TOUCHES': 1}),
    ('bias=any+вход в середине', {'BIAS_MODE': 'any', 'POI_ENTRY_DEPTH': 0.5}),

    # ── Комбинации-кандидаты ───────────────────────────────────────────
    ('bias=any+OB+conf4.0+RR3',  {'BIAS_MODE': 'any', 'POI_TYPES_ENABLED': ('ORDER_BLOCK',),
                                  'MIN_CONFLUENCE_SCORE': 4.0, 'MIN_RR': 3.0}),
    ('bias=4H+OB+conf4.0',       {'BIAS_MODE': 'htf_only',
                                  'POI_TYPES_ENABLED': ('ORDER_BLOCK',),
                                  'MIN_CONFLUENCE_SCORE': 4.0}),
]

EXTRA_TRACKED = TRACKED + ['KILLZONE_AS_GATE', 'BIAS_MODE']


def portfolio_on(orders, exec_data, start=None, end=None):
    """Портфель на подпериоде: фильтруем ордера по дате создания."""
    subset = orders
    if start is not None:
        subset = [o for o in subset if o.created >= start]
    if end is not None:
        subset = [o for o in subset if o.created < end]
    if not subset:
        return None
    return run_portfolio(
        subset, exec_data, risk_pct=RISK_PCT, max_positions=MAX_POSITIONS,
        cooldown_hours=COOLDOWN_HOURS,
        breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
        max_hold_hours=P.MAX_POSITION_HOLD_HOURS)


def run_configs(configs, pairs, title='УСТОЙЧИВОСТЬ ПО ПОЛУГОДИЯМ'):
    """
    Считает набор конфигураций и печатает таблицу с разбивкой по полугодиям.

    Вынесено из main(), чтобы другие исследовательские скрипты (например,
    smc_improve.py) переиспользовали ту же машинерию, а не заводили копию:
    расхождение копий уже один раз дорого обошлось этому проекту.
    """
    print(f'Загрузка {len(pairs)} пар...', flush=True)
    data = {}
    for pair in pairs:
        loaded = load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    if not data:
        print('Нет данных.')
        return []

    print('Построение контекстов...', flush=True)
    contexts = {}
    for pair, frames in data.items():
        contexts[pair] = smc_signal.build_context(
            {'bias': frames['1d'], 'htf': frames['4h'], 'poi': frames['1h']}, pair=pair)
        print(f'   {pair}: готов', flush=True)

    exec_data = {pair: frames['5m'] for pair, frames in data.items()}

    any_df = next(iter(data.values()))['1h']
    first = pd.Timestamp(any_df['timestamp'].iloc[0]).tz_convert('UTC').tz_localize(None)
    last = pd.Timestamp(any_df['timestamp'].iloc[-1]).tz_convert('UTC').tz_localize(None)
    midpoint = np.datetime64(first + (last - first) / 2)
    print(f'Период: {first.date()} .. {last.date()}, '
          f'граница половин: {midpoint}', flush=True)
    print(flush=True)

    defaults = {key: deepcopy(getattr(P, key)) for key in EXTRA_TRACKED}
    rows = []

    for name, overrides in configs:
        for key, value in defaults.items():
            setattr(P, key, value)
        for key, value in overrides.items():
            setattr(P, key, value)

        orders = []
        for pair, ctx in contexts.items():
            orders += build_orders(ctx, pair, data[pair]['1h'])

        full = portfolio_on(orders, exec_data)
        half1 = portfolio_on(orders, exec_data, end=midpoint)
        half2 = portfolio_on(orders, exec_data, start=midpoint)

        stats_full = compute_stats(full, label=name) if full else None
        if not stats_full:
            print(f'   {name:<26} сделок нет', flush=True)
            continue
        stats_h1 = compute_stats(half1, label='H1') if half1 else None
        stats_h2 = compute_stats(half2, label='H2') if half2 else None

        rows.append({
            'name': name, 'orders': len(orders),
            'trades': stats_full['trades'], 'ret': stats_full['return_pct'],
            'dd': stats_full['max_dd_pct'], 'wr': stats_full['winrate'],
            'pf': stats_full['profit_factor'], 'sumr': stats_full['sum_r'],
            'h1': stats_h1['return_pct'] if stats_h1 else 0.0,
            'h2': stats_h2['return_pct'] if stats_h2 else 0.0,
            'h1_pf': stats_h1['profit_factor'] if stats_h1 else 0.0,
            'h2_pf': stats_h2['profit_factor'] if stats_h2 else 0.0,
        })
        print(f'   {name:<26} год={rows[-1]["ret"]:+7.1f}%  '
              f'H1={rows[-1]["h1"]:+7.1f}%  H2={rows[-1]["h2"]:+7.1f}%  '
              f'PF={rows[-1]["pf"]:.3f}  n={rows[-1]["trades"]}', flush=True)

    print()
    print('=' * 104)
    print(f'{title}  (принимаем только прибыльные в ОБЕИХ половинах)')
    print('=' * 104)
    header = (f'{"конфигурация":<28}{"сделок":>8}{"год%":>9}{"DD%":>7}{"WR%":>7}'
              f'{"PF":>8}{"H1%":>9}{"H2%":>9}{"H1 PF":>8}{"H2 PF":>8}{"вердикт":>10}')
    print(header)
    print('-' * len(header))
    for row in sorted(rows, key=lambda r: -r['sumr']):
        verdict = 'устойчив' if row['h1'] > 0 and row['h2'] > 0 else 'подгонка'
        print(f'{row["name"]:<28}{row["trades"]:>8}{row["ret"]:>+9.1f}{row["dd"]:>7.1f}'
              f'{row["wr"]:>7.1f}{row["pf"]:>8.3f}{row["h1"]:>+9.1f}{row["h2"]:>+9.1f}'
              f'{row["h1_pf"]:>8.3f}{row["h2_pf"]:>8.3f}{verdict:>10}')
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pairs', default=','.join(BASE_PAIRS))
    args = parser.parse_args()
    run_configs(CONFIGS, [p.strip() for p in args.pairs.split(',') if p.strip()])


if __name__ == '__main__':
    main()
