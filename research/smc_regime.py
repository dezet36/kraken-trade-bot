"""
Проверка стратегий на другом рыночном режиме.

Весь остальной бэктест проекта сделан на 2025-05 .. 2026-07 — периоде
преимущественно бычьем, где обе стратегии почти всё время держали лонги.
Стратегия может показывать отличный profit factor на росте и терять депозит
на развороте, и по бычьим данным это не видно.

Здесь период 2022-01 .. 2023-07, охватывающий три режима подряд:
    2022 H1 — обвал (крах LUNA в мае, каскад ликвидаций);
    2022 H2 — продолжение падения и крах FTX в ноябре;
    2023 H1 — восстановление и длинный боковик.

Разбивка идёт по этим трём отрезкам отдельно: усреднение по всему периоду
спрятало бы ровно то, что мы проверяем.

Запуск (после research/fetch_bear_data.py):
    python research/smc_regime.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Кэш переключаем ДО импорта backtest_smc — CACHE_DIR читается при импорте
os.environ.setdefault(
    'SMC_CACHE_DIR', os.path.join(ROOT, 'research', 'backtest_cache_bear'))

from smc import params as P, signal as smc_signal  # noqa: E402
from smc_engine import compute_stats, run_portfolio  # noqa: E402
from backtest_smc import (COOLDOWN_HOURS, RISK_PCT, fibo_orders,  # noqa: E402
                          load_pair)
from smc_sweep import build_orders  # noqa: E402

PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT',
         'LINKUSDT', 'BNBUSDT', 'AVAXUSDT', 'LTCUSDT', 'DOTUSDT', 'BCHUSDT',
         'UNIUSDT', 'XLMUSDT']

# Границы режимов внутри периода
SEGMENTS = [
    ('обвал 2022 H1',   '2022-01-01', '2022-07-01'),
    ('дно + FTX 2022 H2', '2022-07-01', '2023-01-01'),
    ('боковик 2023 H1', '2023-01-01', '2023-07-01'),
    ('ВЕСЬ ПЕРИОД',     '2022-01-01', '2023-07-01'),
]


def portfolio(orders, exec_data, start=None, end=None, slots=5, cap=None):
    subset = orders
    if start is not None:
        subset = [o for o in subset if o.created >= start]
    if end is not None:
        subset = [o for o in subset if o.created < end]
    if not subset:
        return None
    return run_portfolio(
        subset, exec_data, risk_pct=RISK_PCT, max_positions=slots,
        cooldown_hours=COOLDOWN_HOURS,
        breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
        max_hold_hours=P.MAX_POSITION_HOLD_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION if cap is None else cap)


def main():
    print(f'Кэш: {os.environ["SMC_CACHE_DIR"]}')
    print(f'Загрузка {len(PAIRS)} пар...', flush=True)

    data = {}
    for pair in PAIRS:
        loaded = load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
        else:
            print(f'   {pair}: нет данных', flush=True)
    if not data:
        print('Кэш пуст — сначала прогони research/fetch_bear_data.py')
        return

    available = list(data)
    span = data[available[0]]['1h']
    print(f'   пар: {len(available)}, период '
          f'{span.timestamp.iloc[0].date()} .. {span.timestamp.iloc[-1].date()}',
          flush=True)

    exec_data = {pair: data[pair]['5m'] for pair in available}

    print('\n[SMC] построение контекстов...', flush=True)
    contexts = {}
    for pair in available:
        contexts[pair] = smc_signal.build_context({
            'bias': data[pair]['1d'], 'htf': data[pair]['4h'], 'poi': data[pair]['1h'],
        }, pair=pair)

    # Варианты SMC. Фильтр режима (подтверждённый тренд) и направленный кэп
    # проверяются именно здесь: на бычьих данных они не нужны, а провал был
    # на дне и в боковике.
    variants = [
        ('SMC база',       {'BIAS_REQUIRE_CONFIRMED': False}, 0),
        ('SMC подтв.',     {'BIAS_REQUIRE_CONFIRMED': True}, 0),
        ('SMC подтв.+кэп3', {'BIAS_REQUIRE_CONFIRMED': True}, 3),
    ]

    strategies = []
    generated = {}
    for name, overrides, cap in variants:
        for key, value in overrides.items():
            setattr(P, key, value)
        cache_key = tuple(sorted(overrides.items()))
        if cache_key not in generated:
            orders = []
            for pair in available:
                orders += build_orders(contexts[pair], pair, data[pair]['1h'])
            generated[cache_key] = orders
            print(f'   {name}: {len(orders)} сетапов', flush=True)
        strategies.append((name, generated[cache_key], cap))

    print('[Фибо] генерация сетапов...', flush=True)
    fibo = []
    for pair in available:
        fibo += fibo_orders(pair, data[pair])
    print(f'   сетапов: {len(fibo)}', flush=True)
    strategies.append(('Фибо', fibo, 0))

    print()
    print('=' * 96)
    print('ПОВЕДЕНИЕ НА МЕДВЕЖЬЕМ РЫНКЕ И В БОКОВИКЕ')
    print('=' * 96)
    header = (f'{"режим":<22}{"стратегия":<17}{"сделок":>8}{"доход%":>10}'
              f'{"DD%":>8}{"WR%":>7}{"PF":>8}{"R/сдел":>9}')
    print(header)
    print('-' * len(header))

    for name, start, end in SEGMENTS:
        s = np.datetime64(pd.Timestamp(start))
        e = np.datetime64(pd.Timestamp(end))
        for label, orders, cap in strategies:
            result = portfolio(orders, exec_data, start=s, end=e, cap=cap)
            if not result or not result['trades']:
                print(f'{name:<22}{label:<17}{"нет сделок":>8}')
                continue
            stats = compute_stats(result, label=label)
            print(f'{name:<22}{label:<17}{stats["trades"]:>8}'
                  f'{stats["return_pct"]:>+10.1f}{stats["max_dd_pct"]:>8.1f}'
                  f'{stats["winrate"]:>7.1f}{stats["profit_factor"]:>8.3f}'
                  f'{stats["expectancy_r"]:>9.3f}')
        print('-' * len(header))

    print('\nКритерий годности для реальных денег: стратегия не должна терять')
    print('депозит ни в одном из режимов. Прибыль только на росте — это ставка')
    print('на рынок, а не торговая система.')


if __name__ == '__main__':
    main()
