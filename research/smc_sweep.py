"""
Перебор параметров SMC-стратегии.

Тяжёлый контекст (структура, зоны, свипы, имбалансы) строится ОДИН раз на
пару и переиспользуется: перебираются только решающие параметры, которые на
контекст не влияют. Без этого перебор считался бы часами.

Параметры модулей читаются во время вызова (`params.X`), поэтому подмена
атрибута модуля меняет поведение всего ядра без перезагрузки.

Запуск:
    python research/smc_sweep.py
    python research/smc_sweep.py --pairs BTCUSDT,ETHUSDT,SOLUSDT
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
from smc_engine import INITIAL_BALANCE, Order, compute_stats, run_portfolio  # noqa: E402
from backtest_smc import (COOLDOWN_HOURS, MAX_POSITIONS, RISK_PCT,  # noqa: E402
                          load_pair)

BASE_PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'LINKUSDT', 'AVAXUSDT']


def build_orders(ctx, pair, df):
    """Сетапы одного контекста при ТЕКУЩИХ значениях параметров."""
    orders = []
    seen = set()
    expiry = np.timedelta64(int(P.PENDING_ORDER_MAX_HOURS * 3600), 's')
    # Длительность свечи рабочего ТФ: контекст уже посчитал её по медиане
    # интервалов, что устойчиво к пропускам свечей в данных биржи.
    bar_ns = ctx._durations.get('poi') or 0

    for i in range(60, len(df)):
        setup, _ = ctx.evaluate(i, balance=INITIAL_BALANCE)
        if setup is None:
            continue
        poi = setup['poi']
        key = (pair, poi['type'], poi['index'], setup['direction'])
        if key in seen:
            continue
        seen.add(key)

        # Момент создания ордера — ЗАКРЫТИЕ свечи сигнала, а не её открытие.
        # setup['time'] хранит время открытия (соглашение ccxt), и без сдвига
        # ордер начинал искать налив на час раньше, чем существовало решение.
        # На текущих настройках это не стреляло (зону с касанием отсеивает
        # POI_MAX_TOUCHES=0), но включённый отступ входа сразу оживил бы
        # ловушку — и подглядывание было бы незаметным.
        created = np.datetime64(
            pd.Timestamp(setup['time']).tz_convert('UTC').tz_localize(None)
            + pd.Timedelta(bar_ns, unit='ns'))
        trade = setup['params']
        orders.append(Order(
            pair=pair, direction=setup['direction'],
            entry=trade['entry'], stop=trade['stop_loss'],
            targets=trade['targets'], fractions=trade['fractions'],
            created=created, expires=created + expiry, key=key,
            meta={
                'poi_type': poi['type'],
                'confluence': setup['confluence'],
                'rr': trade['rr'],
                # Богатый контекст для разбора вкладов: какие факторы реально
                # предсказывают результат, а какие оказались декорацией.
                'factors': dict(setup['factors']),
                'direction': setup['direction'],
                'impulse_pct': poi.get('impulse_pct') or 0.0,
                'sweep': (setup['sweep'] or {}).get('source'),
                'rr_first': trade['rr_first'],
                'sl_pct': trade['sl_distance'] / trade['entry'] if trade['entry'] else 0.0,
                'hour': int(pd.Timestamp(setup['time']).tz_convert('UTC').hour),
                'leg_bars': setup['leg']['end']['index'] - setup['leg']['start']['index'],
            },
        ))
    return orders


def evaluate_config(name, overrides, contexts, data, defaults):
    """Применяет набор параметров, прогоняет портфель, возвращает статистику."""
    for key, value in defaults.items():
        setattr(P, key, value)
    for key, value in overrides.items():
        setattr(P, key, value)

    orders = []
    for pair, ctx in contexts.items():
        orders += build_orders(ctx, pair, data[pair]['1h'])

    exec_data = {pair: frames['5m'] for pair, frames in data.items()}
    outcome = run_portfolio(
        orders, exec_data, risk_pct=RISK_PCT, max_positions=MAX_POSITIONS,
        cooldown_hours=COOLDOWN_HOURS,
        breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
        max_hold_hours=P.MAX_POSITION_HOLD_HOURS)

    stats = compute_stats(outcome, label=name)
    stats['orders'] = len(orders)
    stats['no_fill'] = outcome['skipped']['no_fill']
    return stats


# ── Сетка конфигураций: по одному фактору от базы + несколько комбинаций ──────
CONFIGS = [
    ('база',                {}),
    ('только OB',           {'POI_TYPES_ENABLED': ('ORDER_BLOCK',)}),
    ('все зоны',            {'POI_TYPES_ENABLED': ()}),
    ('стоп >=1%',           {'MIN_SL_PCT': 0.010}),
    ('стоп >=1.5%',         {'MIN_SL_PCT': 0.015}),
    ('стоп агрессивный',    {'SL_MODE': 'aggressive'}),
    ('confluence 4.5',      {'MIN_CONFLUENCE_SCORE': 4.5}),
    ('confluence 5.5',      {'MIN_CONFLUENCE_SCORE': 5.5}),
    ('RR>=2',               {'MIN_RR': 2.0}),
    ('RR>=4',               {'MIN_RR': 4.0}),
    ('один тейк',           {'TP_CLOSE_FRACTIONS': (1.0,)}),
    ('тейк 50/50',          {'TP_CLOSE_FRACTIONS': (0.5, 0.5)}),
    ('без killzone',        {'REQUIRE_KILLZONE': False}),
    ('вход в середине зоны', {'POI_ENTRY_DEPTH': 0.5}),
    ('1 касание зоны ок',   {'POI_MAX_TOUCHES': 1}),
    ('без premium/discount', {'REQUIRE_PREMIUM_DISCOUNT': False}),
    ('без безубытка',       {'BREAKEVEN_AFTER_TP1': False}),
    ('OB + стоп 1% + 1 тейк',
     {'POI_TYPES_ENABLED': ('ORDER_BLOCK',), 'MIN_SL_PCT': 0.010,
      'TP_CLOSE_FRACTIONS': (1.0,)}),
    ('OB + стоп 1.5% + conf 4.5',
     {'POI_TYPES_ENABLED': ('ORDER_BLOCK',), 'MIN_SL_PCT': 0.015,
      'MIN_CONFLUENCE_SCORE': 4.5}),
    ('OB + агрессивный стоп + RR2',
     {'POI_TYPES_ENABLED': ('ORDER_BLOCK',), 'SL_MODE': 'aggressive',
      'MIN_RR': 2.0}),
]

TRACKED = ['POI_TYPES_ENABLED', 'MIN_SL_PCT', 'SL_MODE', 'MIN_CONFLUENCE_SCORE',
           'MIN_RR', 'TP_CLOSE_FRACTIONS', 'REQUIRE_KILLZONE', 'POI_ENTRY_DEPTH',
           'POI_MAX_TOUCHES', 'REQUIRE_PREMIUM_DISCOUNT', 'BREAKEVEN_AFTER_TP1',
           'MAX_POSITION_HOLD_HOURS', 'PENDING_ORDER_MAX_HOURS']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pairs', default=','.join(BASE_PAIRS))
    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(',') if p.strip()]

    print(f'Загрузка {len(pairs)} пар...')
    data = {}
    for pair in pairs:
        loaded = load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
        else:
            print(f'   {pair}: нет кэша')
    if not data:
        return

    print('Построение контекстов (один раз на пару)...')
    contexts = {}
    for pair, frames in data.items():
        contexts[pair] = smc_signal.build_context(
            {'bias': frames['1d'], 'htf': frames['4h'], 'poi': frames['1h']}, pair=pair)
        print(f'   {pair}: POI={len(contexts[pair].pois)}, свипов={len(contexts[pair].sweeps)}')

    defaults = {key: deepcopy(getattr(P, key)) for key in TRACKED}

    results = []
    for name, overrides in CONFIGS:
        stats = evaluate_config(name, overrides, contexts, data, defaults)
        results.append(stats)
        print(f'   {name:<28} сделок={stats["trades"]:4d}  '
              f'доход={stats["return_pct"]:+7.1f}%  DD={stats["max_dd_pct"]:5.1f}%  '
              f'WR={stats["winrate"]:4.1f}%  PF={stats["profit_factor"]:.3f}  '
              f'sumR={stats["sum_r"]:+6.1f}')

    print('\n' + '=' * 96)
    print('РЕЙТИНГ ПО СУММЕ R (устойчивее к размеру риска, чем доходность)')
    print('=' * 96)
    header = (f'{"конфигурация":<30}{"сетапов":>9}{"сделок":>8}{"доход%":>9}'
              f'{"DD%":>7}{"WR%":>7}{"PF":>8}{"sumR":>8}')
    print(header)
    print('-' * len(header))
    for stats in sorted(results, key=lambda s: -s['sum_r']):
        print(f'{stats["label"]:<30}{stats["orders"]:>9}{stats["trades"]:>8}'
              f'{stats["return_pct"]:>+9.1f}{stats["max_dd_pct"]:>7.1f}'
              f'{stats["winrate"]:>7.1f}{stats["profit_factor"]:>8.3f}'
              f'{stats["sum_r"]:>+8.1f}')


if __name__ == '__main__':
    main()
