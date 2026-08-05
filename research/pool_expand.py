"""
Замер расширения пула: помогает ли больше пар, и какой ценой.

ЗАЧЕМ. Пул из 16 пар даёт стратегии уровней 20-30 сделок в месяц. Все
рычаги количества внутри самой стратегии проверены и отвергнуты — каждый
стоил качества хотя бы на одном периоде. Число независимых инструментов
остаётся единственным, что не трогает логику сигнала.

ЧТО МЕРЯЕТСЯ. Три пула, каждый на всех трёх стратегиях:

    16   нынешний
    21   плюс пять пар с историей, покрывающей ОБА периода
    30   плюс девять молодых (торгуются с 2023-24) — их можно проверить
         ТОЛЬКО на бычьем периоде

ЧТО ЭТО ЗНАЧИТ ДЛЯ ВЫВОДОВ. Пул из 21 проверяется двусторонне, как всё
остальное в этой работе. Пул из 30 — только на бычьем периоде, и это
принципиально слабее: за сессию односторонняя проверка трижды оказывалась
ложной, а дважды конфигурация с лучшим результатом на быке была почти
худшей на медведе. Результат по пулу из 30 — не доказательство, а
основание для наблюдения в фантоме.

ОПАСНОСТЬ РАСШИРЕНИЯ, которую надо померить отдельно. Больше пар — больше
одновременных позиций и выше суммарный риск. В крипте пары ходят за
биткоином, и десять «разных» сделок в одну сторону — это одна ставка
десятикратным размером. Поэтому в отчёте есть просадка и максимум
одновременных позиций, а не только доход.

Запуск:
    python research/pool_expand.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_engine import compute_stats, run_portfolio  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, ci, load_period)

RNG = np.random.default_rng(20260805)
BOOTSTRAP = 10_000

CURRENT = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT',
           'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT', 'ZECUSDT', 'SUIUSDT',
           'ARBUSDT', 'DOTUSDT', 'XLMUSDT', 'SHIB1000USDT']

# История покрывает оба периода — проверяются двусторонне.
VALIDATED = ['NEARUSDT', 'UNIUSDT', 'AAVEUSDT', 'COTIUSDT', 'BICOUSDT']

# Торгуются с 2023-24: только бычий период.
YOUNG = ['ONDOUSDT', 'ENAUSDT', '1000PEPEUSDT', 'WLDUSDT', 'TAOUSDT',
         'LDOUSDT', 'HYPEUSDT', '1000RATSUSDT', 'HFTUSDT']

POOLS = [
    ('нынешний (16)', CURRENT),
    ('+5 проверяемых (21)', CURRENT + VALIDATED),
    ('+9 молодых (30)', CURRENT + VALIDATED + YOUNG),
]


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def levels_orders(period, pairs):
    from levels import core, params as LP
    from smc_engine import Order
    orders = []
    for pair in pairs:
        data = period['data'].get(pair)
        if data is None:
            continue
        df = data['1h']
        ts = pd.to_datetime(df['timestamp'])
        if getattr(ts.dt, 'tz', None) is not None:
            ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
        ts = ts.to_numpy(dtype='datetime64[ns]')
        high = df['high'].to_numpy(dtype=float)
        low = df['low'].to_numpy(dtype=float)
        close = df['close'].to_numpy(dtype=float)
        volume = (df['volume'].to_numpy(dtype=float)
                  if 'volume' in df.columns else np.ones(len(df)))
        levels = core.build_levels(high, low)
        atr_values = core.atr(high, low, close)
        bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
        expiry = np.timedelta64(int(LP.EXPIRY_HOURS * 3600), 's')
        seen = set()
        for i in range(60, len(close)):
            setup, _ = core.evaluate(high, low, close, volume, i,
                                     levels=levels, atr_values=atr_values)
            if setup is None:
                continue
            key = (pair, round(setup['level'], 8), setup['direction'],
                   setup['pierce_index'])
            if key in seen:
                continue
            seen.add(key)
            created = ts[i] + np.timedelta64(bar_ns, 'ns')
            orders.append(Order(
                pair=pair, direction=setup['direction'], entry=setup['entry'],
                stop=setup['stop_loss'], targets=[setup['target']],
                fractions=[1.0], created=created, expires=created + expiry,
                key=key, entry_type='stop', meta={}))
    return orders


def smc_orders(period, pairs):
    from smc_sweep import build_orders
    orders = []
    for pair in pairs:
        ctx = period['contexts'].get(pair)
        if ctx is None:
            continue
        orders += build_orders(ctx, pair, period['data'][pair]['1h'])
    return orders


def run(period, pairs, orders, strategy):
    from levels import params as LP
    from smc import params as P
    bt = period['bt']
    exec_data = {p: period['data'][p]['5m'] for p in pairs if p in period['data']}
    if strategy == 'LEVELS':
        result = run_portfolio(orders, exec_data, risk_pct=LP.RISK_PCT,
                               max_positions=LP.MAX_POSITIONS,
                               cooldown_hours=LP.COOLDOWN_HOURS,
                               max_same_direction=LP.MAX_SAME_DIRECTION,
                               max_hold_hours=LP.MAX_HOLD_HOURS,
                               breakeven_after_tp1=False)
    else:
        result = run_portfolio(orders, exec_data, risk_pct=bt.RISK_PCT,
                               max_positions=bt.MAX_POSITIONS,
                               cooldown_hours=bt.COOLDOWN_HOURS,
                               max_same_direction=P.MAX_SAME_DIRECTION)
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = [{'r': t['pnl'] / t['risk'],
             'pair': t['pair'],
             'entry_time': pd.Timestamp(t['entry_time'])}
            for t in result['trades'] if t.get('risk')]
    stats['rows'] = pd.DataFrame(rows)
    stats['orders'] = len(orders)
    return stats


def main():
    periods = [
        load_period(BULL_CACHE, sorted(set(CURRENT + VALIDATED + YOUNG)),
                    'бычий 2025-26'),
        load_period(BEAR_CACHE, sorted(set(CURRENT + VALIDATED)),
                    'медвежий 2022-23'),
    ]
    months = {'бычий 2025-26': 14, 'медвежий 2022-23': 18}
    results = {}

    for period in periods:
        have = set(period['data'])
        for pool_name, pool in POOLS:
            pairs = [p for p in pool if p in have]
            if pool_name == '+9 молодых (30)' and period['label'].startswith('медв'):
                continue          # молодых на медвежьем нет по построению
            for strategy, builder in (('SMC', smc_orders), ('LEVELS', levels_orders)):
                orders = builder(period, pairs)
                stats = run(period, pairs, orders, strategy)
                if stats is None:
                    continue
                results[(period['label'], pool_name, strategy)] = (stats, len(pairs))
                df = stats['rows']
                print(f'   [{period["label"]}] {pool_name} {strategy}: пар {len(pairs)}, '
                      f'{len(df)} сделок, {stats["return_pct"]:+.1f}%, '
                      f'DD {stats["max_dd_pct"]:.1f}%', flush=True)

    for strategy in ('SMC', 'LEVELS'):
        print()
        print('=' * 104)
        print(f'{strategy}')
        print('=' * 104)
        head = (f'{"период":<18}{"пул":<22}{"пар":>5}{"сделок":>8}{"в месяц":>9}'
                f'{"R/сделку":>10}{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"доход/DD":>10}')
        print(head)
        print('-' * len(head))
        for period in periods:
            label = period['label']
            for pool_name, _ in POOLS:
                item = results.get((label, pool_name, strategy))
                if not item:
                    continue
                stats, npairs = item
                df, dd = stats['rows'], stats['max_dd_pct']
                print(f'{label:<18}{pool_name:<22}{npairs:>5}{len(df):>8}'
                      f'{len(df) / months[label]:>9.1f}{df.r.mean():>10.3f}'
                      f'{df.r.sum():>9.1f}{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
                      f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}')
            base = results.get((label, POOLS[0][0], strategy))
            if not base:
                continue
            for pool_name, _ in POOLS[1:]:
                item = results.get((label, pool_name, strategy))
                if not item:
                    continue
                (lo, hi), p = diff_ci(item[0]['rows'].r, base[0]['rows'].r)
                verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
                print(f'   {label} / {pool_name}: ΔR '
                      f'{item[0]["rows"].r.mean() - base[0]["rows"].r.mean():+.3f} '
                      f'[{lo:+.3f}; {hi:+.3f}] -> {verdict}')

    print()
    print('=' * 104)
    print('ВКЛАД НОВЫХ ПАР ПО ОТДЕЛЬНОСТИ (стратегия уровней, бычий период)')
    print('=' * 104)
    item = results.get(('бычий 2025-26', '+9 молодых (30)', 'LEVELS'))
    if item:
        df = item[0]['rows']
        for group, name in ((VALIDATED, 'проверяемые'), (YOUNG, 'молодые'),
                            (CURRENT, 'нынешние')):
            sub = df[df.pair.isin(group)]
            if len(sub) < 5:
                continue
            lo, hi = ci(sub.r)
            print(f'   {name:<14}{len(sub):>5} сделок  R/сделку {sub.r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  сумма {sub.r.sum():+.1f}')
        print()
        worst = df.groupby('pair').r.agg(['count', 'sum']).sort_values('sum')
        print('   худшие пары:', ', '.join(
            f'{p} ({row["sum"]:+.1f}R за {int(row["count"])})'
            for p, row in worst.head(5).iterrows()))
        print('   лучшие пары:', ', '.join(
            f'{p} ({row["sum"]:+.1f}R за {int(row["count"])})'
            for p, row in worst.tail(5).iloc[::-1].iterrows()))

    print()
    print('ПРИЁМКА. Пул из 21 — двусторонняя, как всё остальное. Пул из 30 —')
    print('только бычий период, и это основание для наблюдения в фантоме, а')
    print('не доказательство.')


if __name__ == '__main__':
    main()
