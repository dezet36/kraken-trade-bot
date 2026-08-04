"""
Круг 14: размер позиции по режиму рынка — и честный, без заглядывания вперёд.

Разбор направлений дал главный факт всей серии:

    трендовые режимы (рост + падение):   252 сделки    +5.6 R
    боковик:                             580 сделок  +224.9 R

30% сделок приносят 2% прибыли. Направление при этом определяется верно —
в падении 69-81% сделок шорты, в росте 81-82% лонги. Просто в выраженном
тренде цена не возвращается в зону интереса, и вся конструкция работает
вхолостую, неся при этом полный риск.

Отсюда идея: не запрещать торговлю в тренде, а уменьшать в нём размер.
Количество сделок сохраняется — падает только вклад бесполезной части в
просадку.

ВАЖНО ПРО ЧЕСТНОСТЬ ЗАМЕРА. В разборах режим размечался порогом, взятым как
квантиль по ВСЕМУ периоду, — для описания это допустимо, для торгового
правила нет: живой бот такого порога не знает. Здесь порог считается по
расширяющемуся окну, только по уже виденным дням, и до накопления
MIN_HISTORY дней режим считается неизвестным (множитель 1.0 — как сейчас).
Поэтому цифры этого круга НЕ обязаны совпасть с разборами, и совпадать не
должны.

Запуск:
    python research/smc_v14.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, ER_TREND_QUANTILE, ER_WINDOW,
                               REGIMES, _naive, ci, load_period)

MIN_HISTORY = 180   # дней ER, накопленных ДО первой оценки порога

RNG = np.random.default_rng(20260804)
BOOTSTRAP = 10_000

# ('имя', режим, множитель)
#   'trend'   — множитель применяется только в трендовых режимах
#   'uniform' — ко ВСЕМ сделкам; это контроль, без которого нельзя утверждать,
#               что работает адресность. Любое сокращение риска механически
#               уменьшает просадку, и если равномерное даёт то же отношение
#               дохода к просадке, значит измерялось «торговать меньше», а не
#               «торговать меньше там, где нет преимущества».
CONFIGS = [
    ('база (без деления)',   'trend',   None),
    ('в тренде 0.75',        'trend',   0.75),
    ('в тренде 0.50',        'trend',   0.50),
    ('в тренде 0.25',        'trend',   0.25),
    ('в тренде 0 (запрет)',  'trend',   0.0),
    # Контроли. 0.90 примерно равен среднему риску варианта «в тренде 0.50»
    # (трендовых сделок 13-23%), 0.75 и 0.50 показывают, куда идёт отношение
    # при равномерном сокращении вообще.
    ('равномерно 0.90',      'uniform', 0.90),
    ('равномерно 0.75',      'uniform', 0.75),
    ('равномерно 0.50',      'uniform', 0.50),
]


def causal_regime(daily):
    """
    Режим на каждый день ТОЛЬКО по прошлым данным.

    Отличие от разборной версии одно, и оно принципиальное: порог
    направленности берётся как квантиль по значениям ER, известным на этот
    день, а не по всему периоду. Пока накоплено меньше MIN_HISTORY значений,
    режим не определён — правило обязано молчать там, где живой бот ещё не
    имел бы статистики.
    """
    close = daily['close'].to_numpy(dtype=float)
    times = pd.to_datetime(daily['timestamp'])
    if getattr(times.dt, 'tz', None) is not None:
        times = times.dt.tz_convert('UTC').dt.tz_localize(None)

    steps = np.abs(np.diff(close, prepend=close[0]))
    er = np.full(len(close), np.nan)
    move = np.full(len(close), np.nan)
    for i in range(ER_WINDOW, len(close)):
        moved = close[i] - close[i - ER_WINDOW]
        path = steps[i - ER_WINDOW + 1:i + 1].sum()
        move[i] = moved
        er[i] = abs(moved) / path if path > 0 else 0.0

    labels = [None] * len(close)
    seen = []
    for i in range(len(close)):
        if np.isnan(er[i]):
            continue
        if len(seen) >= MIN_HISTORY:
            threshold = float(np.quantile(seen, ER_TREND_QUANTILE))
            if er[i] < threshold:
                labels[i] = 'боковик'
            else:
                labels[i] = 'рост' if move[i] > 0 else 'падение'
        seen.append(er[i])       # день добавляется ПОСЛЕ использования
    return times.to_numpy(dtype='datetime64[ns]'), labels


def make_lookup(times, labels):
    def lookup(when):
        idx = int(np.searchsorted(times, _naive(when).to_datetime64(), 'right')) - 1
        if idx < 0 or idx >= len(labels):
            return None
        return labels[idx]
    return lookup


def build_period(period):
    """Ордера строятся один раз: параметры сигнала в этом круге не меняются."""
    from smc_sweep import build_orders
    orders = []
    for pair in period['data']:
        orders += build_orders(period['contexts'][pair], pair,
                               period['data'][pair]['1h'])
    times, labels = causal_regime(period['data']['BTCUSDT']['1d'])
    known = [x for x in labels if x]
    print(f'   [{period["label"]}] ордеров: {len(orders)}, '
          f'дней с известным режимом: {len(known)} '
          f'(боковик {known.count("боковик") / max(len(known), 1):.0%})', flush=True)
    return orders, make_lookup(times, labels)


def run_with(period, orders, lookup, kind, factor):
    from smc import params as P
    from smc_engine import compute_stats, run_portfolio
    bt = period['bt']

    def scale(order):
        if factor is None:
            return 1.0
        if kind == 'uniform':
            return factor
        reg = lookup(order.created)
        # Неизвестный режим — полный размер: правило не имеет права
        # додумывать то, чего на тот момент никто не знал.
        return 1.0 if reg in (None, 'боковик') else factor

    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION,
        risk_scale=scale)
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        reason = str(t.get('exit_reason', ''))
        rows.append({
            'r': t['pnl'] / t['risk'],
            'regime': lookup(t['entry_time']),
            'scale': t.get('risk_scale', 1.0),
            'direction': 'LONG' if t.get('direction') in ('BULLISH', 'LONG') else 'SHORT',
            'tps': int(reason[-1]) if reason and reason[-1].isdigit() else 0,
        })
    stats['rows'] = pd.DataFrame(rows)
    return stats


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    prepared = {p['label']: build_period(p) for p in periods}
    results = {}

    for name, kind, factor in CONFIGS:
        for period in periods:
            orders, lookup = prepared[period['label']]
            stats = run_with(period, orders, lookup, kind, factor)
            if stats is None:
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: {len(df)} сделок, '
                  f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%, '
                  f'сумма R {df.r.sum():+.1f}', flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 100)
        print(label.upper())
        print('=' * 100)
        head = (f'{"конфигурация":<22}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
                f'{"доход%":>9}{"DD%":>7}{"доход/DD":>10}{"в тренде":>10}')
        print(head)
        print('-' * len(head))
        for name, _, _f in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            in_trend = df.regime.isin(('рост', 'падение')).mean() * 100
            dd = stats['max_dd_pct']
            ratio = stats['return_pct'] / dd if dd else float('nan')
            print(f'{name:<22}{len(df):>8}{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{dd:>7.1f}{ratio:>10.2f}'
                  f'{in_trend:>9.0f}%')

        base = results.get((label, CONFIGS[0][0]))
        if not base:
            continue
        print()
        print('Разница с базой (интервал через ноль = разница недоказуема):')
        for name, _, _f in CONFIGS[1:]:
            stats = results.get((label, name))
            if not stats:
                continue
            (lo, hi), p = diff_ci(stats['rows'].r, base['rows'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<22} ΔR {stats["rows"].r.mean() - base["rows"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 100)
    print('СРЕДНИЙ R ПО РЕЖИМАМ ПРИ ЧЕСТНОЙ (ПРИЧИННОЙ) РАЗМЕТКЕ')
    print('=' * 100)
    base_frames = [results[(p['label'], CONFIGS[0][0])]['rows'] for p in periods
                   if (p['label'], CONFIGS[0][0]) in results]
    merged = pd.concat(base_frames, ignore_index=True)
    head = f'{"режим":<14}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}{"интервал":>24}'
    print(head)
    print('-' * len(head))
    for reg in list(REGIMES) + [None]:
        sub = merged[merged.regime.isna()] if reg is None else merged[merged.regime == reg]
        if len(sub) < 3:
            continue
        lo, hi = ci(sub.r)
        title = reg or 'режим неизвестен'
        print(f'{title:<14}{len(sub):>8}{sub.r.mean():>10.3f}{sub.r.sum():>9.1f}'
              f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}')
    print()
    print('Контроль читается по колонке «доход/DD»: адресность доказана только')
    print('если «в тренде X» обходит равномерные варианты с тем же средним')
    print('риском. Иначе измерено просто уменьшение размера.')
    print()
    print('Если разница «боковик против тренда» уцелела на причинной разметке —')
    print('находка настоящая. Если растворилась — она была следствием порога,')
    print('подсмотренного в будущем, и правило строить не на чем.')


if __name__ == '__main__':
    main()
