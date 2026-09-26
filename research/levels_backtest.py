"""
Замер стратегии уровней НА БОЕВОМ ЯДРЕ.

Отличие от levels_gerchik.py принципиальное: там была вторая, отдельная
реализация той же идеи, и она разошлась с боевой. Разошлась молча — замер
показывал +90%, а живой бот не мог выдать ни одного сигнала, потому что
исследовательская версия искала подтверждение ВПЕРЁД от текущей свечи, а
живому боту вперёд смотреть некуда.

Здесь вызывается ровно то, что торгует: Live_Bot/levels/core.evaluate.
Портфельные настройки берутся из Live_Bot/levels/params. Разойтись больше
нечему — реализация одна.

УРОВНИ — ПО ОКНУ, КАК В БОЮ (26.09.2026). До этого build_levels звался ОДИН
раз по всей истории, и в решение на свече i протекало будущее: касания,
случившиеся позже, склеивались с прошлыми, а уровень, которому ещё предстояло
получить касание, прятался до того момента. Бот так не может — он строит
уровни каждый час по последним LOOKBACK+4 закрытым свечам. Точная симуляция
(levels_live_sim.py) на одних свечах и одних правилах брокера:

    период             прежний замер              как в бою
    2022-23 падение    224 сд, +0.434R/сд        132 сд, +0.378R/сд
    2025-26 падение    290 сд, +0.348R/сд        201 сд, +0.036R/сд
    2025-11…2026-09     56 сд, +0.540R/сд         44 сд, −0.416R/сд

Числа в levels/params (сумма R по порогу MIN_TARGET_R и т.п.) сняты прежним
способом и к бою отношения не имеют. causal=False — прежний способ, только
для сравнения. Страж — tests/test_levels_backtest_is_causal.py.

Запуск:
    python research/levels_backtest.py
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


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def live_window():
    """Сколько закрытых свечей видит бой: LOOKBACK+5 запрошено, идущая отброшена."""
    return LP.LOOKBACK + 4


def decide(high, low, close, volume, i, causal=True, full=None):
    """
    Решение на свече i. causal — как в бою: уровни и ATR по окну из
    live_window() свечей, кончающемуся на i. Иначе — по всей истории (full —
    заранее посчитанные (уровни, ATR)), то есть с будущим; только для
    сравнения. Индексы в ответе — по всей серии.
    """
    if not causal:
        levels, atr_values = full if full is not None else (core.build_levels(high, low),
                                                             core.atr(high, low, close))
        return core.evaluate(high, low, close, volume, i, levels=levels, atr_values=atr_values)
    lo = max(0, i - live_window() + 1)
    h, l, c, v = high[lo:i + 1], low[lo:i + 1], close[lo:i + 1], volume[lo:i + 1]
    setup, why = core.evaluate(h, l, c, v, len(c) - 1,
                               levels=core.build_levels(h, l), atr_values=core.atr(h, l, c))
    if setup is not None:
        setup = dict(setup)
        for key in ('reclaim_index', 'pierce_index', 'first_index'):
            if key in setup:
                setup[key] = int(setup[key]) + lo
    return setup, why


def build_orders(pair, df, causal=True):
    """
    Ордера по паре: боевая evaluate вызывается на КАЖДОЙ свече — по окну,
    как в бою (см. decide). causal=False — прежний способ с будущим.
    """
    ts = pd.to_datetime(df['timestamp'])
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    ts = ts.to_numpy(dtype='datetime64[ns]')
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    volume = (df['volume'].to_numpy(dtype=float) if 'volume' in df.columns
              else np.ones(len(df)))

    full = None if causal else (core.build_levels(high, low), core.atr(high, low, close))
    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
    expiry = np.timedelta64(int(LP.EXPIRY_HOURS * 3600), 's')

    orders, seen = [], set()
    for i in range(60, len(df)):
        setup, _ = decide(high, low, close, volume, i, causal=causal, full=full)
        if setup is None:
            continue
        # Один прокол одного уровня торгуется один раз.
        key = (pair, round(setup['level'], 8), setup['direction'],
               setup['pierce_index'])
        if key in seen:
            continue
        seen.add(key)

        # Решение принято на ЗАКРЫТИИ свечи возврата — ордер живёт с этого
        # момента, не раньше.
        created = ts[i] + np.timedelta64(bar_ns, 'ns')
        orders.append(Order(
            pair=pair, direction=setup['direction'],
            entry=setup['entry'], stop=setup['stop_loss'],
            targets=[setup['target']], fractions=[1.0],
            created=created, expires=created + expiry, key=key,
            # Вход по рынку на закрытии свечи возврата: цена уже там,
            # движок нальёт на первой свече исполнения и возьмёт тейкерскую
            # комиссию, как и положено рыночному входу.
            entry_type='stop',
            meta={'touches': setup['touches'], 'mirror': setup['mirror'],
                  'volume_ratio': setup['volume_ratio'], 'rr': setup['rr']},
        ))
    return orders


def run(period, orders):
    # Слотов без предела — как в бою (settings_store: max_slots 0).
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=LP.RISK_PCT, max_positions=99,
        cooldown_hours=LP.COOLDOWN_HOURS,
        max_same_direction=LP.MAX_SAME_DIRECTION,
        max_hold_hours=LP.MAX_HOLD_HOURS,
        breakeven_after_tp1=False)
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
            'volume_ratio': t['meta'].get('volume_ratio', 0),
            'entry_time': pd.Timestamp(t['entry_time']),
            'days': (pd.Timestamp(t['exit_time']) - pd.Timestamp(t['entry_time'])
                     ).total_seconds() / 86400,
        })
    stats['rows'] = pd.DataFrame(rows)
    stats['orders'] = len(orders)
    return stats


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]

    print()
    print('=' * 96)
    print('СТРАТЕГИЯ УРОВНЕЙ НА БОЕВОМ ЯДРЕ')
    print('=' * 96)
    print(f'настройки: объём >= {LP.VOLUME_RATIO}x, уровней {LP.NEAREST_LEVELS}, '
          f'касаний {LP.MIN_TOUCHES}, стоп >= {LP.MIN_STOP_PCT}%, '
          f'цель >= {LP.MIN_TARGET_R}R')
    print(f'портфель: слотов без предела, кулдаун {LP.COOLDOWN_HOURS} ч, '
          f'риск {LP.RISK_PCT}%')
    print()
    head = (f'{"период":<20}{"заявок":>8}{"сделок":>8}{"винрейт":>9}{"R/сделку":>10}'
            f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"доход/DD":>10}{"дней":>7}')
    print(head)
    print('-' * len(head))

    frames = {}
    for period in periods:
        orders = []
        for pair, data in period['data'].items():
            orders += build_orders(pair, data['1h'])
        stats = run(period, orders) if orders else None
        if stats is None:
            print(f'{period["label"]:<20}{len(orders):>8}   сделок нет')
            continue
        frames[period['label']] = stats
        df = stats['rows']
        dd = stats['max_dd_pct']
        print(f'{period["label"]:<20}{stats["orders"]:>8}{len(df):>8}'
              f'{(df.r > 0).mean() * 100:>8.0f}%{df.r.mean():>10.3f}'
              f'{df.r.sum():>9.1f}{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
              f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}'
              f'{df.days.median():>7.1f}')

    for label, stats in frames.items():
        df = stats['rows']
        print()
        print(f'{label}:')
        for side in ('LONG', 'SHORT'):
            sub = df[df.direction == side]
            if len(sub) < 5:
                continue
            lo, hi = ci(sub.r)
            print(f'   {side:<6}{len(sub):>5} сделок  винрейт {(sub.r > 0).mean() * 100:>3.0f}%  '
                  f'R/сделку {sub.r.mean():+.3f}  [{lo:+.3f}; {hi:+.3f}]')
        month = df.set_index('entry_time').resample('MS').r.agg(['count', 'sum'])
        month = month[month['count'] > 0]
        if len(month):
            print(f'   прибыльных месяцев: {(month["sum"] > 0).mean() * 100:.0f}% '
                  f'из {len(month)}')

    if frames:
        print()
        print('=' * 96)
        print('ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
        print('=' * 96)
        merged = pd.concat([s['rows'] for s in frames.values()], ignore_index=True)
        for reg in REGIMES:
            sub = merged[merged.regime == reg]
            if len(sub) < 3:
                continue
            lo, hi = ci(sub.r)
            print(f'   {reg:<10}{len(sub):>5} сделок  R/сделку {sub.r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    main()
