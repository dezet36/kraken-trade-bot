"""
Помогает ли SMC подтверждение входа — то, что перевернуло стратегию уровней.

ОТКУДА ГИПОТЕЗА. За сессию нашлась одна необъяснённая асимметрия. У SMC и
фибо покупки убыточны во ВСЕХ срезах — на двух периодах и в трёх режимах
рынка:

    SMC, лонги:  рост -0.181, падение -0.094, боковик +0.268
    SMC, шорты:  рост +0.357, падение +0.137, боковик +0.491

А у стратегии уровней лонги прибыльны: +0.037 и +0.160 на тех же периодах.

Что отличает уровни от SMC по способу входа — ровно одно. SMC ставит лимит
в зоне и заполняется, ПОКА ЦЕНА ПАДАЕТ: заявка исполняется тем вернее, чем
сильнее падение. Уровни не входят, пока рынок не показал реакцию — прокол
с возвратом. На стратегии уровней это оказалось решающим: без
подтверждения -1104 R, с ним +138 R на том же наборе уровней.

ГИПОТЕЗА: дело не в стороне сделки, а в том, что лимит на покупку в
падающем рынке исполняется всегда, а на продажу в растущем — тоже, и обе
стороны страдают одинаково; но падения в крипте быстрее и глубже, поэтому
у лонгов это заметнее.

ЧТО ПРОВЕРЯЕТСЯ. Тот же набор сетапов SMC, но вход другой:

    limit    как сейчас: лимит на границе зоны;
    reclaim  цена должна ЗАЙТИ в зону и закрыться обратно за её дальней
             границей в пределах N свечей; вход по закрытию свечи возврата,
             стоп за экстремум захода.

Приёмка двусторонняя: сумма R не ниже ни на одном периоде И просадка не
выше. Отдельно смотрим лонги и шорты: если гипотеза верна, подтверждение
должно вытянуть именно покупки.

Запуск:
    python research/smc_confirm.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period)

RNG = np.random.default_rng(20260805)
BOOTSTRAP = 10_000
BULLISH = 'BULLISH'


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def confirm_entry(high, low, close, start, zone_edge, zone_far, is_long,
                  window):
    """
    Ищет заход в зону с возвратом.

    Возвращает (индекс свечи возврата, экстремум захода) или None.

    Смысл ровно тот же, что у стратегии уровней: не входить, пока рынок не
    показал, что зона держит. Разница с лимитом принципиальная — лимит
    заполняется тем вернее, чем сильнее движение против нас.
    """
    entered_at = None
    for k in range(start, min(start + window + 1, len(close))):
        touched = low[k] <= zone_edge if is_long else high[k] >= zone_edge
        if touched:
            entered_at = k
            break
    if entered_at is None:
        return None

    extreme = low[entered_at] if is_long else high[entered_at]
    for k in range(entered_at, min(entered_at + window + 1, len(close))):
        extreme = min(extreme, low[k]) if is_long else max(extreme, high[k])
        back = close[k] > zone_edge if is_long else close[k] < zone_edge
        if back and k > entered_at:
            return k, float(extreme)
    return None


def build_orders(ctx, pair, df, mode='limit', window=6, pad_pct=0.1):
    """Сетапы SMC с выбранным способом входа."""
    from smc import params as P
    from smc_sweep import build_orders as smc_orders

    base = smc_orders(ctx, pair, df)
    if mode == 'limit':
        return base

    ts = pd.to_datetime(df['timestamp'])
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    ts = ts.to_numpy(dtype='datetime64[ns]')
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0

    out = []
    for order in base:
        # Свеча, на которой сетап стал известен
        idx = int(np.searchsorted(ts, order.created, 'right')) - 1
        if idx < 0 or idx + 1 >= len(close):
            continue
        is_long = order.direction in (BULLISH, 'LONG')

        found = confirm_entry(high, low, close, idx + 1, order.entry,
                              order.stop, is_long, window)
        if found is None:
            continue
        r_at, extreme = found

        entry = float(close[r_at])
        # Стоп за экстремум захода плюс запас: цена туда уже сходила и
        # вернулась, значит уровень там и проходит.
        pad = entry * pad_pct / 100
        stop = extreme - pad if is_long else extreme + pad
        dist = abs(entry - stop)
        if dist <= 0:
            continue

        # Цели остаются те же по цене — меняется только точка входа, поэтому
        # RR пересчитывается сам собой и его ухудшение будет видно в замере.
        targets = list(order.targets)
        ahead = [t for t in targets
                 if (t > entry if is_long else t < entry)]
        if not ahead:
            continue

        created = ts[r_at] + np.timedelta64(bar_ns, 'ns')
        fractions = list(order.fractions)[:len(ahead)]
        if fractions:
            fractions[-1] += 1.0 - sum(fractions)
        else:
            fractions = [1.0]

        out.append(Order(
            pair=pair, direction=order.direction, entry=entry,
            stop=float(stop), targets=ahead, fractions=fractions,
            created=created, expires=order.expires, key=order.key,
            entry_type='stop', meta=dict(order.meta or {}),
        ))
    return out


def run(period, mode, window=6):
    from smc import params as P
    bt = period['bt']
    orders = []
    for pair in period['data']:
        orders += build_orders(period['contexts'][pair], pair,
                               period['data'][pair]['1h'], mode=mode,
                               window=window)
    if not orders:
        return None
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION)
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
            'direction': 'LONG' if t['direction'] in (BULLISH, 'LONG') else 'SHORT',
        })
    stats['rows'] = pd.DataFrame(rows)
    stats['orders'] = len(orders)
    return stats


CONFIGS = [
    ('лимит в зоне (как сейчас)', 'limit', 6),
    ('подтверждение, окно 4',     'reclaim', 4),
    ('подтверждение, окно 6',     'reclaim', 6),
    ('подтверждение, окно 10',    'reclaim', 10),
]


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    results = {}
    for name, mode, window in CONFIGS:
        for period in periods:
            stats = run(period, mode, window)
            if stats is None:
                print(f'   [{period["label"]}] {name}: сделок нет', flush=True)
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: заявок {stats["orders"]}, '
                  f'{len(df)} сделок, {stats["return_pct"]:+.1f}%, '
                  f'DD {stats["max_dd_pct"]:.1f}%, сумма R {df.r.sum():+.1f}',
                  flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 100)
        print(label.upper())
        print('=' * 100)
        head = (f'{"вход":<28}{"сделок":>8}{"винрейт":>9}{"R/сделку":>10}'
                f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"доход/DD":>10}')
        print(head)
        print('-' * len(head))
        for name, _, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            dd = stats['max_dd_pct']
            print(f'{name:<28}{len(df):>8}{(df.r > 0).mean() * 100:>8.0f}%'
                  f'{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
                  f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}')

        base = results.get((label, CONFIGS[0][0]))
        if not base:
            continue
        print()
        print('Разница с лимитом (интервал через ноль = недоказуема):')
        for name, _, _ in CONFIGS[1:]:
            stats = results.get((label, name))
            if not stats:
                continue
            (lo, hi), p = diff_ci(stats['rows'].r, base['rows'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<28} ΔR {stats["rows"].r.mean() - base["rows"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 100)
    print('ГЛАВНОЕ: ВЫТЯГИВАЕТ ЛИ ПОДТВЕРЖДЕНИЕ ИМЕННО ЛОНГИ')
    print('=' * 100)
    head = f'{"вход":<28}' + ''.join(f'{s:>30}' for s in ('LONG', 'SHORT'))
    print(head)
    print('-' * len(head))
    for name, _, _ in CONFIGS:
        frames = [results[(p['label'], name)]['rows'] for p in periods
                  if (p['label'], name) in results]
        if not frames:
            continue
        merged = pd.concat(frames, ignore_index=True)
        parts = []
        for side in ('LONG', 'SHORT'):
            sub = merged[merged.direction == side]
            if len(sub) < 10:
                parts.append(f'{"—":>30}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{len(sub):>5} {sub.r.mean():>7.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(30))
        print(f'{name:<28}' + ''.join(parts))

    print()
    print('=' * 100)
    print('ПО РЕЖИМАМ РЫНКА')
    print('=' * 100)
    head = f'{"вход":<28}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _, _ in CONFIGS:
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
        print(f'{name:<28}' + ''.join(parts))


if __name__ == '__main__':
    main()
