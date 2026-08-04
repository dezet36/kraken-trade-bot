"""
Разбор вкладов: что в SMC действительно предсказывает результат.

Все предыдущие попытки улучшения меняли СТРУКТУРУ стратегии по наитию —
цели, таймфрейм, фильтр режима — и шесть из семи провалились. Здесь другой
подход: посмотреть на фактические сделки и выяснить, какие признаки сетапа
статистически связаны с результатом.

Отдельная причина для этой проверки: веса факторов confluence я задал руками
по методичке и ни разу не валидировал. Если часть из них шум или
отрицательные предикторы, отбор сетапов работает хуже случайного, и никакая
надстройка это не исправит.

Считается на ОБОИХ периодах — бычьем и медвежьем. Признак, работающий только
на одном, для реальной торговли бесполезен: именно так выглядит подгонка.

Запуск:
    python research/smc_attribution.py
"""

import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BULL_CACHE = os.path.join(ROOT, 'research', 'backtest_cache_12m')
BEAR_CACHE = os.path.join(ROOT, 'research', 'backtest_cache_bear')

BULL_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'HYPEUSDT',
    'SUIUSDT', '1000PEPEUSDT', 'ADAUSDT', 'ZECUSDT', 'LINKUSDT', 'WIFUSDT',
    'BNBUSDT', 'AVAXUSDT', 'LTCUSDT', 'TAOUSDT', 'DOTUSDT', 'ARBUSDT',
    'BCHUSDT', 'UNIUSDT',
]
BEAR_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT',
    'LINKUSDT', 'BNBUSDT', 'AVAXUSDT', 'LTCUSDT', 'DOTUSDT', 'BCHUSDT',
    'UNIUSDT', 'XLMUSDT',
]


def collect(cache_dir, pairs, label):
    """Прогоняет период и возвращает сделки с их признаками."""
    os.environ['SMC_CACHE_DIR'] = cache_dir
    # backtest_smc кэширует CACHE_DIR при импорте, поэтому перезагружаем
    for module in ('backtest_smc', 'smc_sweep'):
        sys.modules.pop(module, None)

    import backtest_smc as bt
    from smc_sweep import build_orders
    from smc_engine import run_portfolio
    from smc import signal as smc_signal

    print(f'\n[{label}] загрузка {len(pairs)} пар...', flush=True)
    data = {}
    for pair in pairs:
        loaded = bt.load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    available = list(data)
    print(f'   пар: {len(available)}', flush=True)

    orders = []
    for pair in available:
        ctx = smc_signal.build_context({
            'bias': data[pair]['1d'], 'htf': data[pair]['4h'], 'poi': data[pair]['1h'],
        }, pair=pair)
        orders += build_orders(ctx, pair, data[pair]['1h'])
    print(f'   сетапов: {len(orders)}', flush=True)

    result = run_portfolio(
        orders, {p: data[p]['5m'] for p in available},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS)
    print(f'   сделок: {len(result["trades"])}', flush=True)
    return result['trades']


def r_of(trade):
    return trade['pnl'] / trade['risk'] if trade['risk'] else 0.0


def report_binary(trades_by_period, extract, title, note=''):
    """
    Сравнивает средний R там, где признак есть, и там, где его нет.

    Показывает оба периода рядом: признак, работающий только на одном,
    для реальной торговли бесполезен.
    """
    print(f'\n{title}')
    if note:
        print(f'   {note}')
    print(f'   {"признак":<24}' + ''.join(
        f'{p:>26}' for p in trades_by_period))
    print(f'   {"":<24}' + ''.join(f'{"есть / нет  (n есть)":>26}'
                                   for _ in trades_by_period))
    print('   ' + '-' * (24 + 26 * len(trades_by_period)))

    keys = set()
    for trades in trades_by_period.values():
        for trade in trades:
            keys.update(extract(trade))

    for key in sorted(keys):
        line = f'   {str(key):<24}'
        for trades in trades_by_period.values():
            with_f = [r_of(t) for t in trades if key in extract(t)]
            without = [r_of(t) for t in trades if key not in extract(t)]
            if not with_f or not without:
                line += f'{"—":>26}'
                continue
            line += (f'{np.mean(with_f):>+8.3f} /{np.mean(without):>+8.3f}'
                     f'{"(" + str(len(with_f)) + ")":>10}')
        print(line)


def report_buckets(trades_by_period, value_of, edges, title, note=''):
    """Средний R по диапазонам числового признака."""
    print(f'\n{title}')
    if note:
        print(f'   {note}')
    print(f'   {"диапазон":<24}' + ''.join(f'{p:>22}' for p in trades_by_period))
    print('   ' + '-' * (24 + 22 * len(trades_by_period)))

    for lo, hi in zip(edges[:-1], edges[1:]):
        label = f'{lo:g} .. {hi:g}'
        line = f'   {label:<24}'
        for trades in trades_by_period.values():
            vals = [r_of(t) for t in trades if lo <= value_of(t) < hi]
            if len(vals) < 8:
                line += f'{"мало (" + str(len(vals)) + ")":>22}'
            else:
                line += f'{np.mean(vals):>+13.3f}{"(" + str(len(vals)) + ")":>9}'
        print(line)


def report_pairs(trades_by_period):
    """Вклад по парам: работает ли стратегия везде или на нескольких парах."""
    print('\nВКЛАД ПО ПАРАМ (сумма R; отрицательные пары — кандидаты на вылет)')
    stats = defaultdict(dict)
    for period, trades in trades_by_period.items():
        agg = defaultdict(list)
        for trade in trades:
            agg[trade['pair']].append(r_of(trade))
        for pair, values in agg.items():
            stats[pair][period] = (float(np.sum(values)), len(values))

    periods = list(trades_by_period)
    print(f'   {"пара":<16}' + ''.join(f'{p:>22}' for p in periods))
    print('   ' + '-' * (16 + 22 * len(periods)))
    for pair in sorted(stats, key=lambda p: -sum(v[0] for v in stats[p].values())):
        line = f'   {pair:<16}'
        for period in periods:
            if period in stats[pair]:
                total, count = stats[pair][period]
                line += f'{total:>+14.1f}{"(" + str(count) + ")":>8}'
            else:
                line += f'{"—":>22}'
        print(line)


def main():
    trades = {
        'бычий 25-26': collect(BULL_CACHE, BULL_PAIRS, 'бычий'),
        'медвежий 22-23': collect(BEAR_CACHE, BEAR_PAIRS, 'медвежий'),
    }

    print('\n' + '=' * 96)
    print('ЧТО РЕАЛЬНО ПРЕДСКАЗЫВАЕТ РЕЗУЛЬТАТ')
    print('=' * 96)
    print('Признак имеет ценность, только если работает на ОБОИХ периодах.')

    report_binary(
        trades,
        lambda t: {k for k, v in (t['meta'].get('factors') or {}).items() if v},
        'ФАКТОРЫ CONFLUENCE (средний R когда фактор есть / когда нет)',
        'Веса заданы руками по методичке и здесь проверяются впервые.')

    report_binary(
        trades,
        lambda t: {t['meta'].get('sweep')} if t['meta'].get('sweep') else set(),
        'ИСТОЧНИК СНЯТОЙ ЛИКВИДНОСТИ ПЕРЕД ЗОНОЙ')

    report_binary(
        trades,
        lambda t: {t['meta'].get('direction')} if t['meta'].get('direction') else set(),
        'НАПРАВЛЕНИЕ СДЕЛКИ')

    report_buckets(
        trades, lambda t: t['meta'].get('confluence', 0),
        [4.5, 5.0, 5.5, 6.0, 7.0],
        'ПОРОГ CONFLUENCE (растёт ли качество вместе со скором?)',
        'Если связи нет — сам скор бесполезен как мера качества.')

    report_buckets(
        trades, lambda t: t['meta'].get('rr', 0),
        [4, 6, 8, 12, 100],
        'ВЗВЕШЕННЫЙ RR СЕТАПА')

    report_buckets(
        trades, lambda t: t['meta'].get('sl_pct', 0) * 100,
        [0, 0.5, 1.0, 2.0, 5.0, 100],
        'ШИРИНА СТОПА, % ОТ ЦЕНЫ')

    report_buckets(
        trades, lambda t: t['meta'].get('impulse_pct', 0) * 100,
        [0, 1, 2, 4, 8, 100],
        'СИЛА ИМПУЛЬСА ОТ ЗОНЫ, %')

    report_buckets(
        trades, lambda t: t['meta'].get('leg_bars', 0),
        [0, 5, 10, 20, 40, 1000],
        'ДЛИНА ИМПУЛЬСНОЙ НОГИ, СВЕЧЕЙ')

    report_pairs(trades)


if __name__ == '__main__':
    main()
