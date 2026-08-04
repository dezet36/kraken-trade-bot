"""
Подбор пула пар и параметров исполнения для SMC.

Что уже отвергнуто измерениями:
  - ближняя первая цель (фиксация 1/3 на 1R теряет ~35R из 124.5);
  - младший рабочий ТФ (15m рушит R на сделку с 0.356 почти до нуля);
  - ослабление фильтров качества (confluence, RR, согласие bias).

Что осталось: увеличить число сделок, НЕ трогая отбор. Два рычага —
состав пула и доля налива лимитных ордеров.

Наблюдение, из которого растёт гипотеза про пул: profit factor по прошлым
прогонам вёл себя немонотонно — 6 пар дали 1.339, 10 пар 1.427, а 34 пары
снова 1.336. Значит, оптимум по числу пар существует, и добавлять их надо
по убыванию ликвидности, а не подряд.

Контексты строятся ОДИН раз на самый широкий пул и переиспользуются:
подпулы вложены друг в друга, поэтому достаточно фильтровать ордера.

Запуск:
    python research/smc_pool.py
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
from smc_sweep import build_orders  # noqa: E402

# Пары по убыванию медианного суточного оборота, только с полной историей
# (ASTER, LIT, PUMPFUN отброшены: обрезанная история искажает сравнение
# полугодий — в прошлом прогоне на 34 парах они участвовали зря).
RANKED = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'HYPEUSDT',
    'SUIUSDT', '1000PEPEUSDT', 'ADAUSDT', 'ZECUSDT', 'LINKUSDT', 'WIFUSDT',
    'BNBUSDT', 'AVAXUSDT', 'LTCUSDT', 'TAOUSDT', 'DOTUSDT', 'ARBUSDT',
    'BCHUSDT', 'UNIUSDT', '1000BONKUSDT', 'OPUSDT', 'APTUSDT', 'XLMUSDT',
]

POOL_SIZES = [8, 12, 16, 20, 24]
OFFSETS = [0.0, 0.15, 0.30]        # отступ лимита наружу от зоны, доля высоты
SLOTS = [5, 8]                      # одновременных позиций

TRACKED = ['POI_ENTRY_OFFSET']


def main():
    print(f'Загрузка {len(RANKED)} пар...', flush=True)
    data = {}
    for pair in RANKED:
        loaded = load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
        else:
            print(f'   {pair}: нет кэша', flush=True)

    available = [p for p in RANKED if p in data]
    print(f'   доступно: {len(available)}', flush=True)

    print('Построение контекстов (один раз на пару)...', flush=True)
    contexts = {}
    for pair in available:
        contexts[pair] = smc_signal.build_context({
            'bias': data[pair]['1d'], 'htf': data[pair]['4h'], 'poi': data[pair]['1h'],
        }, pair=pair)
    print(f'   готово: {len(contexts)}', flush=True)

    exec_all = {pair: data[pair]['5m'] for pair in available}
    any_df = data[available[0]]['1h']
    first = pd.Timestamp(any_df['timestamp'].iloc[0]).tz_convert('UTC').tz_localize(None)
    last = pd.Timestamp(any_df['timestamp'].iloc[-1]).tz_convert('UTC').tz_localize(None)
    midpoint = np.datetime64(first + (last - first) / 2)

    defaults = {key: deepcopy(getattr(P, key)) for key in TRACKED}
    rows = []

    for offset in OFFSETS:
        for key, value in defaults.items():
            setattr(P, key, value)
        P.POI_ENTRY_OFFSET = offset

        print(f'\n[отступ входа {offset:.2f}] генерация сетапов...', flush=True)
        per_pair = {}
        for pair in available:
            per_pair[pair] = build_orders(contexts[pair], pair, data[pair]['1h'])
        total = sum(len(v) for v in per_pair.values())
        print(f'   сетапов всего: {total}', flush=True)

        for size in POOL_SIZES:
            pool = available[:size]
            orders = [o for pair in pool for o in per_pair[pair]]
            exec_data = {pair: exec_all[pair] for pair in pool}

            for slots in SLOTS:
                full = run_portfolio(
                    orders, exec_data, risk_pct=RISK_PCT, max_positions=slots,
                    cooldown_hours=COOLDOWN_HOURS,
                    breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
                    max_hold_hours=P.MAX_POSITION_HOLD_HOURS)
                if not full['trades']:
                    continue

                h1 = run_portfolio(
                    [o for o in orders if o.created < midpoint], exec_data,
                    risk_pct=RISK_PCT, max_positions=slots,
                    cooldown_hours=COOLDOWN_HOURS,
                    breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
                    max_hold_hours=P.MAX_POSITION_HOLD_HOURS)
                h2 = run_portfolio(
                    [o for o in orders if o.created >= midpoint], exec_data,
                    risk_pct=RISK_PCT, max_positions=slots,
                    cooldown_hours=COOLDOWN_HOURS,
                    breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
                    max_hold_hours=P.MAX_POSITION_HOLD_HOURS)

                stats = compute_stats(full, label=f'{size}п/{slots}сл/о{offset}')
                s1 = compute_stats(h1) if h1 and h1['trades'] else None
                s2 = compute_stats(h2) if h2 and h2['trades'] else None

                rows.append({
                    'name': f'{size:>2}пар {slots}слот отст{offset:.2f}',
                    'orders': len(orders), 'trades': stats['trades'],
                    'nofill': full['skipped']['no_fill'],
                    'ret': stats['return_pct'], 'dd': stats['max_dd_pct'],
                    'wr': stats['winrate'], 'pf': stats['profit_factor'],
                    'exp': stats['expectancy_r'], 'sumr': stats['sum_r'],
                    'h1': s1['return_pct'] if s1 else 0.0,
                    'h2': s2['return_pct'] if s2 else 0.0,
                })
                print(f'   {rows[-1]["name"]}: n={stats["trades"]:4d} '
                      f'год={stats["return_pct"]:+8.1f}% DD={stats["max_dd_pct"]:5.1f}% '
                      f'PF={stats["profit_factor"]:.3f} R/сд={stats["expectancy_r"]:.3f}',
                      flush=True)

    print('\n' + '=' * 112)
    print('ПУЛ, СЛОТЫ И ОТСТУП ВХОДА  (сортировка по сумме R)')
    print('=' * 112)
    header = (f'{"конфигурация":<26}{"сетапов":>9}{"сделок":>8}{"неналив":>9}{"год%":>10}'
              f'{"DD%":>7}{"WR%":>7}{"PF":>8}{"R/сд":>8}{"sumR":>8}{"H1%":>9}{"H2%":>9}')
    print(header)
    print('-' * len(header))
    for row in sorted(rows, key=lambda r: -r['sumr'])[:24]:
        flag = '' if row['h1'] > 0 and row['h2'] > 0 else '  (подгонка)'
        print(f'{row["name"]:<26}{row["orders"]:>9}{row["trades"]:>8}{row["nofill"]:>9}'
              f'{row["ret"]:>+10.1f}{row["dd"]:>7.1f}{row["wr"]:>7.1f}{row["pf"]:>8.3f}'
              f'{row["exp"]:>8.3f}{row["sumr"]:>+8.1f}{row["h1"]:>+9.1f}{row["h2"]:>+9.1f}{flag}')

    print('\nОриентир — фибо на 10 парах: 1168 сделок, +268.8%, DD 21.0%, '
          'PF 1.295, R/сделку 0.117')


if __name__ == '__main__':
    main()
