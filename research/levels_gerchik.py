"""
Стратегия горизонтальных уровней. Самостоятельная, ни на чём нашем не стоит.

ПРИНЦИП. Стратегия не наследует ничего у SMC и фибо: ни поиска экстремумов,
ни порогов риска, ни числа слотов, ни лимита на одну сторону. Все её числа
лежат в levels_params.py и обоснованы из самого метода. Оценивается она
тоже сама по себе — прибыльна ли, какая просадка, воспроизводится ли на
двух независимых периодах, — а не тем, дополняет ли она чужой портфель.

Разбивка по режимам рынка в отчёте есть, но как СПРАВКА о характере
стратегии, а не как условие приёмки.

МЕТОД. Уровень — цена, к которой рынок возвращался несколько раз, возможно
с разных сторон. Вход лимитом на отбой от уровня, стоп вплотную за ним,
цель кратна риску.

Формализуемые критерии силы уровня:

    касания        сколько раз цена разворачивалась на этой цене
    зеркальность   уровень работал и как потолок, и как пол
    круглое число  психологический уровень
    скорость подхода   быстро пришли — ждём отбой; сползали — ждём пробой

Из метода НЕ воспроизводится биржевой стакан («плотность», крупная лимитная
заявка). Ни в кэше, ни в истории ccxt его нет, и восстановить по свечам
невозможно. Проверяется формализуемая часть.

ЧЕСТНОСТЬ. Экстремум подтверждается через PIVOT_N баров после себя, уровень
известен с подтверждения последнего входящего в него касания. Скорость
подхода и ATR считаются по барам до свечи решения включительно.

Запуск:
    python research/levels_gerchik.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import levels_params as LP  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period)

LONG, SHORT = 'LONG', 'SHORT'


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


def pivots(df, n):
    """
    Точки касания: локальные экстремумы с n барами по обе стороны.

    Своя реализация, а не заимствованная. Здесь важно ровно одно свойство —
    момент, когда экстремум СТАНОВИТСЯ ИЗВЕСТЕН: это n баров после него.
    Уровень, построенный по экстремуму раньше этого момента, знал бы будущее.
    """
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    out = []
    for i in range(n, len(df) - n):
        window_h = high[i - n:i + n + 1]
        window_l = low[i - n:i + n + 1]
        if high[i] == window_h.max() and (window_h.argmax() == n):
            out.append({'index': i, 'price': float(high[i]), 'kind': 'high',
                        'known_at': i + n})
        if low[i] == window_l.min() and (window_l.argmin() == n):
            out.append({'index': i, 'price': float(low[i]), 'kind': 'low',
                        'known_at': i + n})
    return sorted(out, key=lambda p: p['index'])


def round_distance_pct(price):
    """
    Близость цены к круглому числу, в процентах.

    Шаг круглости — на порядок мельче самой цены: для 62 800 это 1 000, для
    0.85 это 0.01. Без привязки к порядку «круглое число» на биткоине и на
    дожкоине значило бы совершенно разное.
    """
    if price <= 0:
        return 100.0
    step = 10.0 ** (np.floor(np.log10(price)) - 1)
    nearest = round(price / step) * step
    return abs(price - nearest) / price * 100


def build_levels(df, tolerance_pct=None, min_touches=None, max_span=None,
                 pivot_n=None):
    """
    Уровни: кластеры касаний, лежащих на одной цене.

    Вершины и низы кладутся в ОДИН пул. Уровень, который сначала
    останавливал рост, а потом держал падение, и есть зеркальный —
    сильнейший по классике. Раздельные пулы такие уровни не видят.
    """
    tolerance_pct = LP.TOLERANCE_PCT if tolerance_pct is None else tolerance_pct
    min_touches = LP.MIN_TOUCHES if min_touches is None else min_touches
    max_span = LP.MAX_SPAN_BARS if max_span is None else max_span
    pivot_n = LP.PIVOT_N if pivot_n is None else pivot_n

    points = pivots(df, pivot_n)
    levels, used = [], set()

    for i, first in enumerate(points):
        if i in used:
            continue
        members, idxs = [first], {i}
        for j in range(i + 1, len(points)):
            second = points[j]
            if second['index'] - first['index'] > max_span:
                break
            if abs(second['price'] - first['price']) / first['price'] * 100 <= tolerance_pct:
                members.append(second)
                idxs.add(j)
        if len(members) < min_touches:
            continue
        used |= idxs
        levels.append({
            'price': float(np.mean([m['price'] for m in members])),
            'touches': len(members),
            'mirror': len({m['kind'] for m in members}) > 1,
            'last_index': members[-1]['index'],
            'known_at': max(m['known_at'] for m in members),
        })
    return levels


def build_orders(pair, df, require_mirror=False, min_touches=None,
                 max_round_pct=None, speed_mode=None, rr_target=None,
                 stop_atr=None, min_stop_pct=None, tolerance_pct=None):
    """Лимитные заявки на отбой от уровня."""
    rr_target = LP.RR_TARGET if rr_target is None else rr_target
    stop_atr = LP.STOP_ATR if stop_atr is None else stop_atr
    min_stop_pct = LP.MIN_STOP_PCT if min_stop_pct is None else min_stop_pct

    ts = pd.to_datetime(df['timestamp'])
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    ts = ts.to_numpy(dtype='datetime64[ns]')
    close = df['close'].to_numpy(dtype=float)
    a = atr(df)
    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
    expiry = np.timedelta64(int(LP.EXPIRY_HOURS * 3600), 's')

    levels = build_levels(df, tolerance_pct=tolerance_pct, min_touches=min_touches)
    if not levels:
        return []
    levels.sort(key=lambda x: x['known_at'])
    known_at = np.array([lv['known_at'] for lv in levels])

    orders, seen = [], set()
    for i in range(60, len(df)):
        if np.isnan(a[i]) or a[i] <= 0:
            continue
        upto = int(np.searchsorted(known_at, i, 'right'))
        if upto == 0:
            continue
        price = close[i]

        if speed_mode:
            j = max(0, i - LP.SPEED_BARS)
            speed = abs(price - close[j]) / a[i]
            if speed_mode == 'fast' and speed < LP.SPEED_THRESHOLD:
                continue
            if speed_mode == 'slow' and speed >= LP.SPEED_THRESHOLD:
                continue

        for lv in levels[:upto]:
            if i - lv['known_at'] > LP.MAX_AGE_BARS:
                continue
            if require_mirror and not lv['mirror']:
                continue
            if max_round_pct is not None and round_distance_pct(lv['price']) > max_round_pct:
                continue

            gap = price - lv['price']
            if abs(gap) > LP.TRIGGER_ATR * a[i] or abs(gap) < LP.MIN_GAP_ATR * a[i]:
                continue

            side = LONG if gap > 0 else SHORT     # цена выше уровня -> поддержка

            # Одна заявка на уровень в сутки: цена может подходить к нему
            # много свечей подряд, и без этого одна и та же идея порождала бы
            # десятки ордеров.
            key = (pair, round(lv['price'], 8), side, int(i // 24))
            if key in seen:
                continue
            seen.add(key)

            entry = float(lv['price'])
            dist = max(stop_atr * a[i], entry * min_stop_pct / 100)
            stop = entry - dist if side == LONG else entry + dist
            target = entry + rr_target * dist if side == LONG else entry - rr_target * dist

            created = ts[i] + np.timedelta64(bar_ns, 'ns')
            orders.append(Order(
                pair=pair, direction=side, entry=entry, stop=float(stop),
                targets=[float(target)], fractions=[1.0],
                created=created, expires=created + expiry, key=key,
                entry_type='limit',
                meta={'touches': lv['touches'], 'mirror': lv['mirror'],
                      'round_pct': round_distance_pct(lv['price'])},
            ))
    return orders


def run(period, orders):
    """Прогон на СОБСТВЕННЫХ портфельных настройках стратегии."""
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=LP.RISK_PCT,
        max_positions=LP.MAX_POSITIONS,
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
            'touches': t['meta'].get('touches', 0),
            'mirror': bool(t['meta'].get('mirror')),
            'days': (pd.Timestamp(t['exit_time']) - pd.Timestamp(t['entry_time'])
                     ).total_seconds() / 86400,
        })
    stats['rows'] = pd.DataFrame(rows)
    stats['orders'] = len(orders)
    return stats


CONFIGS = [
    ('база: 2 касания, RR3',        dict()),
    ('3 касания',                   dict(min_touches=3)),
    ('только зеркальные',           dict(require_mirror=True)),
    ('зеркальные + 3 касания',      dict(require_mirror=True, min_touches=3)),
    ('быстрый подход',              dict(speed_mode='fast')),
    ('МЕДЛЕННЫЙ подход (контроль)', dict(speed_mode='slow')),
    ('круглые числа',               dict(max_round_pct=LP.ROUND_MAX_PCT)),
    ('допуск 0.10% (точное касание)', dict(tolerance_pct=0.10)),
    ('RR 2',                        dict(rr_target=2.0)),
    ('всё вместе',                  dict(require_mirror=True, min_touches=3,
                                         speed_mode='fast', max_round_pct=0.3)),
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
                orders += build_orders(pair, data['1h'], **kw)
            stats = run(period, orders) if orders else None
            if stats is None:
                print(f'   [{period["label"]}] {name}: сделок нет')
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: ордеров {stats["orders"]}, '
                  f'{len(df)} сделок, {stats["return_pct"]:+.1f}%, '
                  f'DD {stats["max_dd_pct"]:.1f}%, сумма R {df.r.sum():+.1f}',
                  flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 104)
        print(label.upper())
        print('=' * 104)
        head = (f'{"конфигурация":<32}{"сделок":>8}{"винрейт":>9}{"R/сделку":>10}'
                f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"доход/DD":>10}{"дней":>7}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            dd = stats['max_dd_pct']
            print(f'{name:<32}{len(df):>8}{(df.r > 0).mean() * 100:>8.0f}%'
                  f'{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
                  f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}'
                  f'{df.days.median():>7.1f}')

    print()
    print('=' * 104)
    print('ПРЕДСКАЗЫВАЮТ ЛИ КРИТЕРИИ СИЛЫ УРОВНЯ (база, оба периода)')
    print('=' * 104)
    print('Это отдельный вопрос: даже если стратегия в целом не взлетит,')
    print('работающий признак имеет ценность сам по себе.')
    frames = [results[(p['label'], 'база: 2 касания, RR3')]['rows'] for p in periods
              if (p['label'], 'база: 2 касания, RR3') in results]
    if frames:
        merged = pd.concat(frames, ignore_index=True)
        for col, title in (('touches', 'касаний'), ('mirror', 'зеркальный')):
            print()
            for value, sub in merged.groupby(col):
                if len(sub) < 10:
                    continue
                lo, hi = ci(sub.r)
                print(f'   {title} = {value}:  {len(sub):>5} сделок  '
                      f'R/сделку {sub.r.mean():+.3f}  [{lo:+.3f}; {hi:+.3f}]')

    print()
    print('=' * 104)
    print('СПРАВОЧНО: ПО РЕЖИМАМ РЫНКА (не критерий приёмки)')
    print('=' * 104)
    head = f'{"конфигурация":<32}' + ''.join(f'{r:>26}' for r in REGIMES)
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
        print(f'{name:<32}' + ''.join(parts))

    print()
    print('ПРИЁМКА: прибыль на ОБОИХ периодах при просадке, с которой можно')
    print('жить. Стратегия оценивается сама по себе.')


if __name__ == '__main__':
    main()
