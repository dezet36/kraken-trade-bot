"""
Перепроверка: сколько стоит вход на уровень БЕЗ подтверждения.

ЗАЧЕМ. В шапке levels_gerchik.py записано: лимит прямо на уровне дал
−1104 R на быке и −1467 R на медведе, а прокол с возвратом — +138 и +112 на
тех же уровнях. Число получено версией скрипта, которой больше нет, и
сторонний разбор 2026-09-19 справедливо спросил, не ошибка ли это.

КАК МЕРЯЕТСЯ. Те же уровни, тот же отбор ближайших и то же окно подхода
(TRIGGER_ATR), что у подтверждённого входа. Разница одна: заявка — лимит
на самом уровне в момент подхода, стоп — за уровнем на ту же глубину, на
какую подтверждённый вход ждёт прокола плюс запас (PIERCE_ATR +
STOP_PAD_ATR), цель — RR_TARGET рисков. Подтверждения нет: заявка стоит
EXPIRY_HOURS и исполняется, если цену довезли до уровня, — и когда он
устоял, и когда его прошли насквозь.

Это НЕ воспроизведение той версии один в один — её нет. Это ответ на
вопрос по существу: меняет ли подтверждение знак результата на тех же
уровнях. Порядок величины важнее третьего знака.

Запуск:
    python research/levels_noconfirm.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import levels_params as LP  # noqa: E402
from levels_gerchik import (LONG, SHORT, atr, build_levels, build_orders,  # noqa: E402
                            round_distance_pct, run)
from smc_engine import Order  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, load_period)


def limit_orders(pair, df):
    """Лимит на уровне при подходе, без ожидания реакции."""
    ts = pd.to_datetime(df['timestamp'])
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    ts = ts.to_numpy(dtype='datetime64[ns]')
    close = df['close'].to_numpy(dtype=float)
    a = atr(df)
    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
    expiry = np.timedelta64(int(LP.EXPIRY_HOURS * 3600), 's')

    levels = build_levels(df, None, None)
    if not levels:
        return []
    levels.sort(key=lambda x: x['known_at'])
    known_at = np.array([lv['known_at'] for lv in levels])
    prices = np.array([lv['price'] for lv in levels])

    orders, seen = [], set()
    for i in range(60, len(df)):
        if np.isnan(a[i]) or a[i] <= 0:
            continue
        upto = int(np.searchsorted(known_at, i, 'right'))
        if upto == 0:
            continue
        price = close[i]
        candidates = [k for k in range(upto)
                      if i - levels[k]['known_at'] <= LP.MAX_AGE_BARS]
        if not candidates:
            continue
        candidates.sort(key=lambda k: abs(prices[k] - price))
        for k in candidates[:LP.NEAREST_LEVELS]:
            lv = levels[k]
            gap = price - lv['price']
            if abs(gap) > LP.TRIGGER_ATR * a[i] or abs(gap) < LP.MIN_GAP_ATR * a[i]:
                continue
            side = LONG if gap > 0 else SHORT
            key = (pair, round(lv['price'], 8), side, int(i // 24))
            if key in seen:
                continue
            seen.add(key)

            entry = float(lv['price'])
            dist = max((LP.PIERCE_ATR + LP.STOP_PAD_ATR) * a[i],
                       entry * LP.MIN_STOP_PCT / 100)
            stop = entry - dist if side == LONG else entry + dist
            target = entry + LP.RR_TARGET * dist if side == LONG else entry - LP.RR_TARGET * dist
            created = ts[i] + np.timedelta64(bar_ns, 'ns')
            orders.append(Order(
                pair=pair, direction=side, entry=entry, stop=float(stop),
                targets=[float(target)], fractions=[1.0],
                created=created, expires=created + expiry, key=key,
                entry_type='limit', be_trigger=None,
                meta={'touches': lv['touches'], 'mirror': lv['mirror'],
                      'round_pct': round_distance_pct(lv['price']),
                      'rr': LP.RR_TARGET}))
    return orders


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    variants = (('лимит на уровне, без подтверждения', limit_orders),
                ('прокол + возврат (боевая логика)', build_orders))
    print()
    head = f'{"вариант":<40}{"период":<20}{"заявок":>8}{"сделок":>8}{"WR":>7}{"R/сделку":>11}{"сумма R":>10}'
    print(head)
    print('-' * len(head))
    for name, builder in variants:
        for period in periods:
            orders = []
            for pair, data in period['data'].items():
                orders += builder(pair, data['1h'])
            stats = run(period, orders) if orders else None
            if stats is None:
                print(f'{name:<40}{period["label"]:<20}{len(orders):>8}   сделок нет')
                continue
            df = stats['rows']
            print(f'{name:<40}{period["label"]:<20}{len(orders):>8}{len(df):>8}'
                  f'{(df.r > 0).mean() * 100:>6.0f}%{df.r.mean():>11.3f}{df.r.sum():>10.1f}',
                  flush=True)
    print()


if __name__ == '__main__':
    main()
