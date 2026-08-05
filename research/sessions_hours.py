"""
Есть ли вообще эффект времени суток: разбивка результата по часу входа.

ПОЧЕМУ ЭТОТ ЗАМЕР, А НЕ ПРЕЖНИЙ. Первая версия гоняла фибо в десяти
конфигурациях (с фильтром, без него и шесть контрольных со случайными
часами). За два с половиной часа она не досчитала даже первую: fibo_orders
вызывает боевую analyze_market на КАЖДОЙ часовой свече каждой пары, это
190 тысяч вызовов на период.

Здесь один прогон БЕЗ фильтра на период, а дальше разбивка уже готовых
сделок по часу входа. На вопрос «есть ли эффект времени» этого достаточно:
если у большинства часов интервал среднего пересекает ноль и знаки
разбросаны случайно, эффекта нет, и блок-лист фиксирует шум конкретной
выборки. Полноценный A/B нужен только если эффект найдётся.

ЧТО ПРОВЕРЯЕТСЯ. В коде стоит BLOCK_ENTRY_HOURS_UTC = {12,13,14,15,16} —
пять часов из двадцати четырёх, американская сессия. Обоснование в
комментарии: бэктест на 6 месяцах и 10 парах, «убыточны в 5 месяцах из 6».
Насторожило то, что часы выбирались тем, что в выборке оказались худшими:
при 24 часах и полугоде данных пять худших найдутся всегда, даже если
время не значит ничего.

Пул сокращён до восьми пар ради скорости. Для вопроса о часе суток это
допустимо: сделок остаётся несколько сотен, а час входа не зависит от
того, какие именно пары в пуле.

Запуск:
    python research/sessions_hours.py
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

PAIRS_LIMIT = 8
BLOCKED = (12, 13, 14, 15, 16)


def fibo_trades(period):
    """Сделки фибо БЕЗ часового фильтра."""
    import config
    from backtest_smc import fibo_orders

    original = config.BLOCK_ENTRY_HOURS_UTC
    config.BLOCK_ENTRY_HOURS_UTC = frozenset()
    try:
        pairs = list(period['data'])[:PAIRS_LIMIT]
        orders = []
        for pair in pairs:
            orders += fibo_orders(pair, period['data'][pair])
            print(f'      {pair}: заявок {len(orders)}', flush=True)
        if not orders:
            return None
        result = run_portfolio(
            orders, {p: period['data'][p]['5m'] for p in pairs},
            risk_pct=config.RISK_PER_TRADE,
            max_positions=config.MAX_ACTIVE_PAIRS,
            cooldown_hours=config.COOLDOWN_HOURS,
            max_same_direction=config.MAX_SAME_DIRECTION)
    finally:
        config.BLOCK_ENTRY_HOURS_UTC = original

    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        entry = pd.Timestamp(t['entry_time'])
        rows.append({'r': t['pnl'] / t['risk'], 'hour': int(entry.hour)})
    stats['rows'] = pd.DataFrame(rows)
    return stats


def by_hour(merged, title):
    print()
    print('=' * 92)
    print(title)
    print('=' * 92)
    print(f'{"час UTC":>8}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
          f'{"интервал среднего":>24}   ')
    print('-' * 78)
    positive = total = 0
    for hour in range(24):
        sub = merged[merged.hour == hour]
        if len(sub) < 8:
            continue
        lo, hi = ci(sub.r)
        total += 1
        positive += 1 if sub.r.mean() > 0 else 0
        mark = '  <- в блок-листе' if hour in BLOCKED else ''
        bar = ('+' if sub.r.sum() >= 0 else '-') * min(int(abs(sub.r.sum())), 18)
        print(f'{hour:>8}{len(sub):>8}{sub.r.mean():>10.3f}{sub.r.sum():>9.1f}'
              f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}   {bar}{mark}')
    print(f'   прибыльных часов: {positive} из {total}')

    blocked = merged[merged.hour.isin(BLOCKED)]
    rest = merged[~merged.hour.isin(BLOCKED)]
    print()
    for name, sub in (('часы 12-16 (блок-лист)', blocked), ('остальные часы', rest)):
        if len(sub) < 10:
            continue
        lo, hi = ci(sub.r)
        print(f'   {name:<26}{len(sub):>5} сделок  R/сделку {sub.r.mean():+.3f}  '
              f'[{lo:+.3f}; {hi:+.3f}]  сумма {sub.r.sum():+.1f}')

    if len(blocked) >= 10 and len(rest) >= 10:
        rng = np.random.default_rng(20260805)
        a = rng.choice(blocked.r.to_numpy(float), (10_000, len(blocked)), True).mean(1)
        b = rng.choice(rest.r.to_numpy(float), (10_000, len(rest)), True).mean(1)
        d = a - b
        lo, hi = np.percentile(d, [2.5, 97.5])
        verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
        print(f'   разность: {d.mean():+.3f}  [{lo:+.3f}; {hi:+.3f}]  -> {verdict}')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    frames = []
    for period in periods:
        print(f'   [{period["label"]}] считаю фибо без фильтра...', flush=True)
        stats = fibo_trades(period)
        if stats is None:
            print(f'   [{period["label"]}] сделок нет')
            continue
        df = stats['rows']
        frames.append(df)
        print(f'   [{period["label"]}] {len(df)} сделок, '
              f'{stats["return_pct"]:+.1f}%, DD {stats["max_dd_pct"]:.1f}%',
              flush=True)
        by_hour(df, f'{period["label"].upper()}: РЕЗУЛЬТАТ ПО ЧАСУ ВХОДА')

    if len(frames) == 2:
        by_hour(pd.concat(frames, ignore_index=True),
                'ОБА ПЕРИОДА ВМЕСТЕ')

    print()
    print('ЧТЕНИЕ. Блок-лист оправдан, только если часы 12-16 хуже остальных')
    print('НА ОБОИХ периодах и разность не пересекает ноль. Иначе он фиксирует')
    print('шум той выборки, на которой был выбран.')


if __name__ == '__main__':
    main()
