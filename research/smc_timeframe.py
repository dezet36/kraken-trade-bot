"""
Проверка раскладок таймфреймов для SMC.

Диагностика (research/smc_diagnose.py) показала, что качество отдельной сделки
у SMC хорошее — 0.39R против 0.137R у фибо-стратегии, — а проигрывает она по
объёму: 321 сделка против 1168 на тех же парах. При этом ослабление фильтров
(confluence, RR, согласие bias) объём добавляет, но качество рушит.

Отсюда гипотеза: нужен не более мягкий отбор, а более младший рабочий
таймфрейм. Та же структурная логика на 15m даёт кратно больше зон интереса,
не трогая ни один фильтр качества.

Раскладка меняет контекст целиком, поэтому переиспользовать построенные
контексты между вариантами нельзя — каждый считается заново.

Запуск:
    python research/smc_timeframe.py
    python research/smc_timeframe.py --pairs BTCUSDT,ETHUSDT,SOLUSDT
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
from smc_engine import compute_stats  # noqa: E402
from backtest_smc import load_pair  # noqa: E402
from smc_sweep import build_orders  # noqa: E402
from smc_validate import portfolio_on  # noqa: E402

PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'LINKUSDT', 'AVAXUSDT']

# (имя, ключи фреймов bias/htf/poi, переопределения параметров)
LADDERS = [
    ('1D/4H/1H (текущая)', ('1d', '4h', '1h'), {}),
    ('1D/4H/15m',          ('1d', '4h', '15m'), {}),
    ('4H/1H/15m',          ('4h', '1h', '15m'), {}),
    ('1D/1H/15m',          ('1d', '1h', '15m'), {}),
    # На младшем ТФ зона живёт меньше свечей по времени, но их количество
    # больше — проверяем, не режет ли возрастной фильтр слишком рано.
    ('4H/1H/15m возраст×3', ('4h', '1h', '15m'), {'POI_MAX_AGE_BARS': 240}),
    ('1D/4H/15m возраст×3', ('1d', '4h', '15m'), {'POI_MAX_AGE_BARS': 240}),
]

TRACKED = ['POI_MAX_AGE_BARS', 'MIN_CONFLUENCE_SCORE', 'MIN_RR',
           'PENDING_ORDER_MAX_HOURS', 'MAX_POSITION_HOLD_HOURS']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pairs', default=','.join(PAIRS))
    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(',') if p.strip()]

    print(f'Загрузка {len(pairs)} пар...', flush=True)
    data = {}
    for pair in pairs:
        loaded = load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    if not data:
        print('Нет данных.')
        return

    sample = next(iter(data.values()))
    print(f'   свечей на пару: 1h={len(sample["1h"])}, 15m={len(sample["15m"])}, '
          f'4h={len(sample["4h"])}, 1d={len(sample["1d"])}', flush=True)

    exec_data = {pair: frames['5m'] for pair, frames in data.items()}

    any_df = sample['1h']
    first = pd.Timestamp(any_df['timestamp'].iloc[0]).tz_convert('UTC').tz_localize(None)
    last = pd.Timestamp(any_df['timestamp'].iloc[-1]).tz_convert('UTC').tz_localize(None)
    midpoint = np.datetime64(first + (last - first) / 2)

    defaults = {key: deepcopy(getattr(P, key)) for key in TRACKED}
    rows = []

    for name, (bias_key, htf_key, poi_key), overrides in LADDERS:
        for key, value in defaults.items():
            setattr(P, key, value)
        for key, value in overrides.items():
            setattr(P, key, value)

        print(f'\n[{name}] построение контекстов...', flush=True)
        orders = []
        for pair, frames in data.items():
            ctx = smc_signal.build_context({
                'bias': frames[bias_key],
                'htf': frames[htf_key],
                'poi': frames[poi_key],
            }, pair=pair)
            orders += build_orders(ctx, pair, frames[poi_key])
        print(f'   сетапов: {len(orders)}', flush=True)

        full = portfolio_on(orders, exec_data)
        if not full:
            print('   сделок нет')
            continue
        half1 = portfolio_on(orders, exec_data, end=midpoint)
        half2 = portfolio_on(orders, exec_data, start=midpoint)

        stats = compute_stats(full, label=name)
        h1 = compute_stats(half1, label='H1') if half1 else None
        h2 = compute_stats(half2, label='H2') if half2 else None

        rows.append({
            'name': name, 'orders': len(orders), 'trades': stats['trades'],
            'ret': stats['return_pct'], 'dd': stats['max_dd_pct'],
            'wr': stats['winrate'], 'pf': stats['profit_factor'],
            'sumr': stats['sum_r'], 'exp': stats['expectancy_r'],
            'nofill': full['skipped']['no_fill'],
            'h1': h1['return_pct'] if h1 else 0.0,
            'h2': h2['return_pct'] if h2 else 0.0,
        })
        print(f'   сделок={stats["trades"]}  доход={stats["return_pct"]:+.1f}%  '
              f'DD={stats["max_dd_pct"]:.1f}%  PF={stats["profit_factor"]:.3f}  '
              f'R/сделку={stats["expectancy_r"]:.3f}  sumR={stats["sum_r"]:+.1f}',
              flush=True)

    print('\n' + '=' * 108)
    print('РАСКЛАДКИ ТАЙМФРЕЙМОВ  (цель — больше сделок БЕЗ потери качества)')
    print('=' * 108)
    header = (f'{"раскладка":<24}{"сетапов":>9}{"сделок":>8}{"неналив":>9}'
              f'{"год%":>9}{"DD%":>7}{"WR%":>7}{"PF":>8}{"R/сдел":>8}'
              f'{"sumR":>8}{"H1%":>8}{"H2%":>8}')
    print(header)
    print('-' * len(header))
    for row in sorted(rows, key=lambda r: -r['sumr']):
        print(f'{row["name"]:<24}{row["orders"]:>9}{row["trades"]:>8}{row["nofill"]:>9}'
              f'{row["ret"]:>+9.1f}{row["dd"]:>7.1f}{row["wr"]:>7.1f}{row["pf"]:>8.3f}'
              f'{row["exp"]:>8.3f}{row["sumr"]:>+8.1f}{row["h1"]:>+8.1f}{row["h2"]:>+8.1f}')

    print('\nОриентир для сравнения — фибо-стратегия на этом же периоде:')
    print('   10 пар: 1168 сделок, +268.8%, DD 21.0%, PF 1.295, R/сделку 0.117')


if __name__ == '__main__':
    main()
