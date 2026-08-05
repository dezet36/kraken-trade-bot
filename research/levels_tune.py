"""
Круг настройки стратегии уровней по результатам диагностики.

ЧТО ПОКАЗАЛА ДИАГНОСТИКА И ОТКУДА ВЗЯТЫ КОНФИГУРАЦИИ

  RR сетапа — сильный предсказатель, известный ДО входа:
      1.5-2   682 сделки   -0.033 R   <- половина оборота, в среднем минус
      2-2.5   248 сделок   +0.203 R   доказуемо
      3-4     141 сделка   +0.352 R   доказуемо
      4+      142 сделки   +0.627 R   доказуемо
    Порог MIN_TARGET_R=1.5 пропускает заведомо худшую половину.

  Объём работает шкалой, а не порогом:
      1.5-2   +0.118      3-4   +0.344 доказуемо      4+   +0.611 доказуемо

  Треть сделок гибнет в первые два часа с -0.461 R. Время удержания
    заранее неизвестно и фильтром быть не может, но средний MAE 0.90
    говорит, что стоп стоит вплотную к шуму. Отсюда варианты с более
    широким стопом и с требованием более глубокого прокола.

ПРИЁМКА. Изменение принимается, только если улучшает ОБА периода. Плюс
интервал разности с базой: пересекает ноль — разница недоказуема.

Запуск:
    python research/levels_tune.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from levels import core, params as LP  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period)

RNG = np.random.default_rng(20260805)
BOOTSTRAP = 10_000

# Свипаемые параметры. Ни один из них не влияет на построение уровней,
# поэтому уровни и ATR считаются один раз на пару и переиспользуются.
TRACKED = ('MIN_TARGET_R', 'VOLUME_RATIO', 'STOP_PAD_ATR', 'MIN_STOP_PCT',
           'PIERCE_ATR', 'MAX_SAME_DIRECTION')

# ПРОВЕРКА УСТОЙЧИВОСТИ ПРИНЯТОГО, а не поиск лучшего числа.
#
# Принято: MIN_TARGET_R=2.0 и MIN_STOP_PCT=1.2. Оба рычага названы
# диагностикой ДО проверки, а не подобраны перебором. Вопрос здесь один:
# держится ли результат при сдвиге настроек. Если хорошо только ровно на
# 1.2% — это подгонка, и честный вывод «стоп должен быть шире», а не
# «стоп ровно 1.2%».
# ВЗАИМОДЕЙСТВИЕ ПОРОГОВ. Порог RR подбирался при стопе 0.8%, потом стоп
# сменился на 1.2% — и оптимум уехал: при широком стопе RR сделки ниже при
# той же цели, значит и порог должен опуститься. Классическая ошибка
# последовательной настройки: каждый параметр подобран верно, пара — нет.
#
# Здесь сетка RR идёт ВНИЗ от 1.75 при новом стопе. Если улучшение
# продолжится к 1.5, порог не нужен вовсе и работает только широкий стоп.
CONFIGS = [
    ('RR 1.50, стоп 1.2%', {'MIN_TARGET_R': 1.50}),
    ('RR 1.60, стоп 1.2%', {'MIN_TARGET_R': 1.60}),
    ('RR 1.75, стоп 1.2%', {'MIN_TARGET_R': 1.75}),
    ('RR 1.90, стоп 1.2%', {'MIN_TARGET_R': 1.90}),
    ('RR 2.00, стоп 1.2%', {}),
    ('RR 1.50, стоп 1.5%', {'MIN_TARGET_R': 1.50, 'MIN_STOP_PCT': 1.5}),
    ('RR 1.75, стоп 1.5%', {'MIN_TARGET_R': 1.75, 'MIN_STOP_PCT': 1.5}),
    ('RR 1.75, стоп 2.0%', {'MIN_TARGET_R': 1.75, 'MIN_STOP_PCT': 2.0}),
]


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def prepare(period):
    """Уровни и ATR по каждой паре — один раз на все конфигурации."""
    cache = {}
    for pair, data in period['data'].items():
        df = data['1h']
        ts = pd.to_datetime(df['timestamp'])
        if getattr(ts.dt, 'tz', None) is not None:
            ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
        high = df['high'].to_numpy(dtype=float)
        low = df['low'].to_numpy(dtype=float)
        close = df['close'].to_numpy(dtype=float)
        volume = (df['volume'].to_numpy(dtype=float) if 'volume' in df.columns
                  else np.ones(len(df)))
        cache[pair] = {
            'ts': ts.to_numpy(dtype='datetime64[ns]'),
            'arrays': (high, low, close, volume),
            'levels': core.build_levels(high, low),
            'atr': core.atr(high, low, close),
        }
    return cache


def build_orders(pair, item):
    high, low, close, volume = item['arrays']
    ts = item['ts']
    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
    expiry = np.timedelta64(int(LP.EXPIRY_HOURS * 3600), 's')

    orders, seen = [], set()
    for i in range(60, len(close)):
        setup, _ = core.evaluate(high, low, close, volume, i,
                                 levels=item['levels'], atr_values=item['atr'])
        if setup is None:
            continue
        key = (pair, round(setup['level'], 8), setup['direction'],
               setup['pierce_index'])
        if key in seen:
            continue
        seen.add(key)
        created = ts[i] + np.timedelta64(bar_ns, 'ns')
        orders.append(Order(
            pair=pair, direction=setup['direction'],
            entry=setup['entry'], stop=setup['stop_loss'],
            targets=[setup['target']], fractions=[1.0],
            created=created, expires=created + expiry, key=key,
            entry_type='stop',
            meta={'rr': setup['rr'], 'volume_ratio': setup['volume_ratio']},
        ))
    return orders


def run(period, cache):
    orders = []
    for pair, item in cache.items():
        orders += build_orders(pair, item)
    if not orders:
        return None
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=LP.RISK_PCT, max_positions=LP.MAX_POSITIONS,
        cooldown_hours=LP.COOLDOWN_HOURS,
        max_same_direction=LP.MAX_SAME_DIRECTION,
        max_hold_hours=LP.MAX_HOLD_HOURS, breakeven_after_tp1=False)
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        rows.append({
            'r': t['pnl'] / t['risk'],
            'regime': period['regime'](t['entry_time']),
            'direction': 'LONG' if t['direction'] in ('BULLISH', 'LONG') else 'SHORT',
            'entry_time': pd.Timestamp(t['entry_time']),
        })
    stats['rows'] = pd.DataFrame(rows)
    stats['orders'] = len(orders)
    return stats


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    caches = {p['label']: prepare(p) for p in periods}
    defaults = {key: getattr(LP, key) for key in TRACKED}
    results = {}

    for name, over in CONFIGS:
        for key, value in defaults.items():
            setattr(LP, key, value)
        for key, value in over.items():
            setattr(LP, key, value)
        for period in periods:
            stats = run(period, caches[period['label']])
            if stats is None:
                print(f'   [{period["label"]}] {name}: сделок нет', flush=True)
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: заявок {stats["orders"]}, '
                  f'{len(df)} сделок, {stats["return_pct"]:+.1f}%, '
                  f'DD {stats["max_dd_pct"]:.1f}%, сумма R {df.r.sum():+.1f}',
                  flush=True)
    for key, value in defaults.items():
        setattr(LP, key, value)

    for period in periods:
        label = period['label']
        print()
        print('=' * 104)
        print(label.upper())
        print('=' * 104)
        head = (f'{"конфигурация":<30}{"сделок":>8}{"винрейт":>9}{"R/сделку":>10}'
                f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"доход/DD":>10}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            dd = stats['max_dd_pct']
            print(f'{name:<30}{len(df):>8}{(df.r > 0).mean() * 100:>8.0f}%'
                  f'{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
                  f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}')

        base = results.get((label, 'RR 2.00, стоп 1.2%'))
        if not base:
            continue
        print()
        print('Разница с текущим RR 2.00 (интервал через ноль = недоказуема):')
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            (lo, hi), p = diff_ci(stats['rows'].r, base['rows'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<30} ΔR {stats["rows"].r.mean() - base["rows"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 104)
    print('ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 104)
    head = f'{"конфигурация":<30}' + ''.join(f'{r:>26}' for r in REGIMES)
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
            if len(sub) < 5:
                parts.append(f'{"—":>26}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{sub.r.mean():>8.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(26))
        print(f'{name:<30}' + ''.join(parts))


if __name__ == '__main__':
    main()
