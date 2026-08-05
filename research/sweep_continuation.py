"""
Третья стратегия: продолжение тренда после снятия ликвидности.

ОТКУДА ИДЕЯ И ПОЧЕМУ ОНА ПОДКРЕПЛЕНА ЗАМЕРОМ. Пробойная стратегия дала
единственное доказуемое различие за всю серию — и отрицательное:

    пробой канала 336ч, режим «рост»:  -0.455 R  [-0.64; -0.26]
    пробой + фильтр 1D, режим «рост»:  -0.471 R  [-0.78; -0.11]

Уход цены за уровень систематически НЕ продолжается. Это ровно то, что
методичка называет снятием ликвидности: за экстремумом стоят стопы, их
собирают, и цена возвращается. Мы измерили сторону, которая теряет.
Здесь проверяется противоположная: не входить на пробое, а дождаться,
когда пробой окажется ложным, и войти на возврате.

ОТЛИЧИЕ ОТ ДЕЙСТВУЮЩЕЙ SMC. Там снятие ликвидности — лишь один из
факторов confluence, а вход всё равно ждёт возврата цены в зону интереса
(ордер-блок). Здесь зона не нужна вовсе: триггером служит само снятие с
возвратом. Поэтому сетапы получаются другие — и по количеству, и по
моменту входа.

ГЛАВНЫЙ ВОПРОС ЗАМЕРА. Формулировка «продолжение тренда после снятия»
предполагает, что снятие происходит ПРОТИВ тренда: в аптренде цена ныряет
под свинг-лоу, собирает стопы и идёт дальше вверх. Но у той же картины
есть зеркало — снятие ПО тренду с разворотом (перехай в аптренде, за ним
падение). Поэтому меряются обе, отдельными строками. Без этого нельзя
понять, работает ли идея продолжения — или просто снятие само по себе.

Запуск:
    python research/sweep_continuation.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc import structure as structure_mod  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period)

BULLISH, BEARISH, NEUTRAL = 'BULLISH', 'BEARISH', 'NEUTRAL'


def atr(df, period=14):
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    out = np.full(len(tr), np.nan)
    if len(tr) > period:
        out[period] = tr[1:period + 1].mean()
        for i in range(period + 1, len(tr)):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def build_orders(ctx, pair, df, mode='continuation', trend_source='poi',
                 stop_buffer=0.25, rr_target=3.0, trail_atr=None,
                 expiry_hours=12.0, min_weight=0.0, min_stop_pct=0.8,
                 entry_at_level=False):
    """
    Ордера по снятию ликвидности.

    mode='continuation'  снятие ПРОТИВ тренда, вход ПО тренду
                         (аптренд: ныряют под лоу -> покупаем)
    mode='reversal'      снятие ПО тренду, вход ПРОТИВ
                         (аптренд: перехай -> продаём)
    mode='any'           направление берётся из самого снятия, тренд не важен

    trend_source  какой структурой считается тренд: 'poi' (1H), 'htf' (4H),
                  'bias' (1D)
    stop_buffer   отступ стопа за экстремум снятия, в долях ATR
    rr_target     цель в R; None вместе с trail_atr — только трейлинг
    min_stop_pct  МИНИМАЛЬНАЯ дистанция стопа в процентах цены. Без неё замер
                  бессмыслен: стоп вплотную за экстремумом прокола выходит
                  тоньше 0.4% на каждом десятом сетапе, а круг издержек
                  (тейкер 0.055% x2 + проскальзывание 0.05% = 0.16% цены)
                  съедает при таком стопе 0.40 R. Первый прогон дал -99% на
                  всех конфигурациях именно поэтому — стратегия проиграла не
                  рынку, а комиссиям. В живом боте этот минимум есть
                  (MIN_SL_PCT), в замере его не было.
    entry_at_level вход лимитом обратно на снятый уровень вместо рынка по
                  закрытию. Комиссия мейкерская вместо тейкерской, но часть
                  сетапов не нальётся.
    """
    ts = pd.to_datetime(df['timestamp'])
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    ts = ts.to_numpy(dtype='datetime64[ns]')
    close = df['close'].to_numpy(dtype=float)
    a = atr(df)
    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
    expiry = np.timedelta64(int(expiry_hours * 3600), 's')

    struct = {'poi': ctx.structure, 'htf': ctx.htf_structure,
              'bias': ctx.bias_structure}.get(trend_source)
    frame = ctx.frames.get(trend_source)
    trend_ts = None
    if struct is not None and frame is not None and trend_source != 'poi':
        t = pd.to_datetime(frame['timestamp'])
        if getattr(t.dt, 'tz', None) is not None:
            t = t.dt.tz_convert('UTC').dt.tz_localize(None)
        trend_ts = t.to_numpy(dtype='datetime64[ns]')

    orders = []
    seen = set()
    for sweep in ctx.sweeps:
        # Снятие СОСТОЯЛОСЬ только когда цена вернулась за уровень. Решение
        # принимается на закрытии этой свечи, не раньше.
        i = sweep['reclaimed_at']
        if i + 1 >= len(df) or np.isnan(a[i]):
            continue
        if sweep.get('weight', 1.0) < min_weight:
            continue

        # Разворотное направление, ожидаемое после снятия
        after = sweep['direction']

        if mode == 'any':
            side = after
        else:
            if struct is None:
                continue
            if trend_source == 'poi':
                idx = i
            else:
                idx = int(np.searchsorted(trend_ts, ts[i], 'right')) - 1
                if idx < 0:
                    continue
            trend = structure_mod.state_at(struct, idx)['trend']
            if trend == NEUTRAL:
                continue
            if mode == 'continuation':
                # Снятие должно быть ПРОТИВ тренда: в аптренде сняли SSL
                # (снизу), после чего ожидается продолжение вверх.
                if after != trend:
                    continue
                side = trend
            else:  # reversal
                # Снятие ПО тренду: в аптренде сняли BSL сверху, ждём разворот
                if after == trend:
                    continue
                side = after

        key = (pair, sweep['side'], sweep['index'], side)
        if key in seen:
            continue
        seen.add(key)

        entry = float(sweep['level']) if entry_at_level else float(close[i])
        extreme = float(sweep['extreme'])
        buf = stop_buffer * a[i]
        stop = extreme - buf if side == BULLISH else extreme + buf
        dist = abs(entry - stop)
        if dist <= 0 or dist > 6 * a[i]:
            # Слишком далёкий стоп — прокол был огромным, риск не считается
            continue

        # Стоп не может быть тоньше минимума: на тонком стопе издержки
        # превращаются в половину риска, и любое преимущество исчезает.
        floor = entry * min_stop_pct / 100
        if dist < floor:
            dist = floor
            stop = entry - dist if side == BULLISH else entry + dist

        if rr_target:
            target = entry + rr_target * dist if side == BULLISH else entry - rr_target * dist
        else:
            target = entry + 1000 * dist if side == BULLISH else entry - 1000 * dist

        created = ts[i] + np.timedelta64(bar_ns, 'ns')
        orders.append(Order(
            pair=pair, direction=side, entry=entry, stop=float(stop),
            targets=[float(target)], fractions=[1.0],
            created=created, expires=created + expiry, key=key,
            # Вход по рынку на закрытии свечи возврата. Тип 'stop' здесь
            # означает «цена уже там», движок нальёт на первой же свече
            # исполнения — и возьмёт тейкерскую комиссию, как и положено
            # рыночному входу.
            entry_type='limit' if entry_at_level else 'stop',
            trail_distance=float(trail_atr * a[i]) if trail_atr else None,
            meta={'source': sweep['source'], 'weight': sweep.get('weight', 1.0),
                  'penetration': sweep['penetration_pct']},
        ))
    return orders


def run(period, orders):
    from smc import params as P
    bt = period['bt']
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION,
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
            'days': (pd.Timestamp(t['exit_time']) - pd.Timestamp(t['entry_time'])
                     ).total_seconds() / 86400,
        })
    stats['rows'] = pd.DataFrame(rows)
    return stats


# Шкала веса пула (smc/liquidity.py): свинг 0.5, PDH/PDL 0.7, PWH/PWL 0.85,
# EQH/EQL 0.9, PMH/PML 1.0. Максимум — единица, поэтому прошлый порог 1.5 не
# пропускал ничего. Фильтр значимости здесь ключевой: без него стратегия
# срабатывает 11-18 тысяч раз за период против 1505 у SMC — столько настоящих
# снятий ликвидности на рынке не бывает, это шум каждого микросвинга.
CONFIGS = [
    ('лимит, пулы >=0.85 (нед/мес/EQ)',
     dict(mode='continuation', trend_source='htf', entry_at_level=True,
          min_weight=0.85)),
    ('лимит, пулы >=0.9 (EQ/мес)',
     dict(mode='continuation', trend_source='htf', entry_at_level=True,
          min_weight=0.9)),
    ('лимит, пулы >=1.0 (только месяц)',
     dict(mode='continuation', trend_source='htf', entry_at_level=True,
          min_weight=1.0)),
    ('лимит, пулы >=0.85, стоп>=1.5%',
     dict(mode='continuation', trend_source='htf', entry_at_level=True,
          min_weight=0.85, min_stop_pct=1.5)),
    ('лимит, пулы >=0.85, RR5',
     dict(mode='continuation', trend_source='htf', entry_at_level=True,
          min_weight=0.85, rr_target=5.0)),
    ('рынок, пулы >=0.85',
     dict(mode='continuation', trend_source='htf', min_weight=0.85)),
    ('РАЗВОРОТ, лимит, пулы >=0.85',
     dict(mode='reversal', trend_source='htf', entry_at_level=True,
          min_weight=0.85)),
    ('лимит, пулы >=0.85, без фильтра тренда',
     dict(mode='any', entry_at_level=True, min_weight=0.85)),
]


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    results = {}
    for name, kw in CONFIGS:
        for period in periods:
            orders = []
            for pair, data in period['data'].items():
                orders += build_orders(period['contexts'][pair], pair,
                                       data['1h'], **kw)
            stats = run(period, orders) if orders else None
            if stats is None:
                print(f'   [{period["label"]}] {name}: сделок нет')
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: ордеров {len(orders)}, '
                  f'{len(df)} сделок, {stats["return_pct"]:+.1f}%, '
                  f'DD {stats["max_dd_pct"]:.1f}%, сумма R {df.r.sum():+.1f}',
                  flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 104)
        print(label.upper())
        print('=' * 104)
        head = (f'{"конфигурация":<38}{"сделок":>8}{"винрейт":>9}{"R/сделку":>10}'
                f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"дней":>7}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            print(f'{name:<38}{len(df):>8}{(df.r > 0).mean() * 100:>8.0f}%'
                  f'{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{stats["max_dd_pct"]:>7.1f}'
                  f'{df.days.median():>7.1f}')

    print()
    print('=' * 104)
    print('ДОПОЛНЯЕМОСТЬ: ГДЕ ОНА СИЛЬНА')
    print('=' * 104)
    print('Текущий портфель: рост -0.083, падение +0.079, боковик +0.388')
    print('Пробой (отклонён): рост -0.13..-0.47, боковик +0.11..+0.22')
    print()
    head = f'{"конфигурация":<38}' + ''.join(f'{r:>26}' for r in REGIMES)
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
            if len(sub) < 3:
                parts.append(f'{"—":>26}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{sub.r.mean():>8.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(26))
        print(f'{name:<38}' + ''.join(parts))


if __name__ == '__main__':
    main()
