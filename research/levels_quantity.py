"""
Круг количества: стратегия уровней стала точнее, но реже.

Принятые пороги (RR>=2.0, стоп>=1.2%) уполовинили просадку и подняли доход,
но сделок стало вдвое меньше:

    было   696 и 633 сделки за 14 и 18 месяцев
    стало  329 и 269   -> примерно 23 и 15 сделок в месяц

Вместе с SMC (около 27 в месяц) портфель получается тонким. Здесь
проверяются рычаги количества, каждый из которых диагностика уже
подсветила как сомнительный ограничитель.

ЧТО ПРОВЕРЯЕТСЯ И ПОЧЕМУ

  MIN_TOUCHES. Число касаний НЕ предсказывает результат — проверено
    дважды: 2 касания +0.144, 3 касания +0.230, 4 касания +0.046,
    5+ +0.098, монотонности нет. Если признак ничего не говорит, требование
    двух касаний просто выбрасывает половину уровней. Проверяем 1 (любой
    свинг), 2 (сейчас) и 3.

  TOLERANCE_PCT. Допуск склейки касаний в один уровень. Шире — уровней
    меньше, но каждый «толще»; уже — уровней больше.

  NEAREST_LEVELS. Сколько ближайших уровней рассматривается за раз.
    Прошлый замер показал нечувствительность, но он делался на дефектном
    ядре и другой конфигурации.

  MAX_AGE_BARS. Насколько старый уровень ещё торгуется.

  RECLAIM_BARS. Окно возврата: шире окно — больше подтверждений
    засчитывается.

ОПАСНОСТЬ, О КОТОРОЙ НАДО ПОМНИТЬ. Ослабление MIN_TOUCHES до единицы
превращает «уровень» в «любой экстремум», и стратегия становится близка к
проверенной ранее ловле снятия ликвидности — та дала чистый ноль. Отличие
здесь в связке «объём на возврате + цель на следующем уровне», и именно
её вклад этот замер и покажет.

ПРИЁМКА. Количество должно вырасти И отношение дохода к просадке не упасть
ни на одном периоде. Больше сделок при худшем качестве — не рост, а
разбавление.

Запуск:
    python research/levels_quantity.py
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

# Эти параметры меняют СОСТАВ уровней, поэтому уровни пересчитываются на
# каждой конфигурации — кэшировать их, как в прошлом круге, здесь нельзя.
TRACKED = ('MIN_TOUCHES', 'TOLERANCE_PCT', 'NEAREST_LEVELS', 'MAX_AGE_BARS',
           'RECLAIM_BARS')

BASE = 'принято (касаний 2, допуск 0.20%)'

CONFIGS = [
    (BASE,                      {}),
    ('касаний 1 (любой свинг)', {'MIN_TOUCHES': 1}),
    ('касаний 3',               {'MIN_TOUCHES': 3}),
    ('допуск 0.30%',            {'TOLERANCE_PCT': 0.30}),
    ('допуск 0.12%',            {'TOLERANCE_PCT': 0.12}),
    ('ближайших уровней 4',     {'NEAREST_LEVELS': 4}),
    ('ближайших уровней 1',     {'NEAREST_LEVELS': 1}),
    ('возраст до 1440 баров',   {'MAX_AGE_BARS': 1440}),
    ('окно возврата 6',         {'RECLAIM_BARS': 6}),
    ('касаний 1 + допуск 0.12%', {'MIN_TOUCHES': 1, 'TOLERANCE_PCT': 0.12}),
]


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def arrays(period):
    out = {}
    for pair, data in period['data'].items():
        df = data['1h']
        ts = pd.to_datetime(df['timestamp'])
        if getattr(ts.dt, 'tz', None) is not None:
            ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
        out[pair] = {
            'ts': ts.to_numpy(dtype='datetime64[ns]'),
            'high': df['high'].to_numpy(dtype=float),
            'low': df['low'].to_numpy(dtype=float),
            'close': df['close'].to_numpy(dtype=float),
            'volume': (df['volume'].to_numpy(dtype=float)
                       if 'volume' in df.columns else np.ones(len(df))),
        }
    return out


def build_orders(pair, item):
    high, low, close, volume = (item['high'], item['low'],
                                item['close'], item['volume'])
    ts = item['ts']
    levels = core.build_levels(high, low)
    atr_values = core.atr(high, low, close)
    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
    expiry = np.timedelta64(int(LP.EXPIRY_HOURS * 3600), 's')

    orders, seen = [], set()
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
            pair=pair, direction=setup['direction'],
            entry=setup['entry'], stop=setup['stop_loss'],
            targets=[setup['target']], fractions=[1.0],
            created=created, expires=created + expiry, key=key,
            entry_type='stop',
            meta={'rr': setup['rr'], 'touches': setup['touches']},
        ))
    return orders, len(levels)


def run(period, data):
    orders, levels_total = [], 0
    for pair, item in data.items():
        pair_orders, count = build_orders(pair, item)
        orders += pair_orders
        levels_total += count
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
            'entry_time': pd.Timestamp(t['entry_time']),
        })
    stats['rows'] = pd.DataFrame(rows)
    stats['orders'] = len(orders)
    stats['levels'] = levels_total
    return stats


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    data = {p['label']: arrays(p) for p in periods}
    months = {'бычий 2025-26': 14, 'медвежий 2022-23': 18}
    defaults = {key: getattr(LP, key) for key in TRACKED}
    results = {}

    for name, over in CONFIGS:
        for key, value in defaults.items():
            setattr(LP, key, value)
        for key, value in over.items():
            setattr(LP, key, value)
        for period in periods:
            stats = run(period, data[period['label']])
            if stats is None:
                print(f'   [{period["label"]}] {name}: сделок нет', flush=True)
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: уровней {stats["levels"]}, '
                  f'заявок {stats["orders"]}, {len(df)} сделок, '
                  f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%',
                  flush=True)
    for key, value in defaults.items():
        setattr(LP, key, value)

    for period in periods:
        label = period['label']
        print()
        print('=' * 110)
        print(label.upper())
        print('=' * 110)
        head = (f'{"конфигурация":<28}{"уровней":>9}{"сделок":>8}{"в месяц":>9}'
                f'{"винрейт":>9}{"R/сделку":>10}{"доход%":>9}{"DD%":>7}{"доход/DD":>10}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            dd = stats['max_dd_pct']
            print(f'{name:<28}{stats["levels"]:>9}{len(df):>8}'
                  f'{len(df) / months[label]:>9.1f}{(df.r > 0).mean() * 100:>8.0f}%'
                  f'{df.r.mean():>10.3f}{stats["return_pct"]:>+9.1f}{dd:>7.1f}'
                  f'{stats["return_pct"] / dd if dd else float("nan"):>10.2f}')

        base = results.get((label, BASE))
        if not base:
            continue
        print()
        print('Разница с принятым (интервал через ноль = разница недоказуема):')
        for name, _ in CONFIGS[1:]:
            stats = results.get((label, name))
            if not stats:
                continue
            (lo, hi), p = diff_ci(stats['rows'].r, base['rows'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'   {name:<28} ΔR {stats["rows"].r.mean() - base["rows"].r.mean():+.3f}  '
                  f'[{lo:+.3f}; {hi:+.3f}]  P(лучше)={p:.0%}  -> {verdict}')

    print()
    print('=' * 110)
    print('ПО РЕЖИМАМ РЫНКА (оба периода вместе)')
    print('=' * 110)
    head = f'{"конфигурация":<28}' + ''.join(f'{r:>26}' for r in REGIMES)
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
        print(f'{name:<28}' + ''.join(parts))

    print()
    print('ПРИЁМКА: сделок больше И отношение дохода к просадке не ниже ни на')
    print('одном периоде. Больше сделок при худшем качестве — разбавление.')


if __name__ == '__main__':
    main()
