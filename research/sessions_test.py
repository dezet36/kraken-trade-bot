"""
Нужны ли временные фильтры: проверка по SMC и по фибо.

ЧТО СТОИТ В КОДЕ СЕЙЧАС

    SMC    KILLZONE_AS_GATE=False, вес фактора killzone = 0.0
           То есть фильтра фактически НЕТ. Оба решения приняты ДО починки
           merge_swings и с тех пор не перепроверялись.

    ФИБО   BLOCK_ENTRY_HOURS_UTC = {12,13,14,15,16} — блокируются пять часов,
           американская сессия. Обоснование: бэктест 6 месяцев / 10 пар,
           «убыточны в 5 месяцах из 6».

ПОЧЕМУ ФИЛЬТР ФИБО ПОДОЗРИТЕЛЕН. Часы выбраны тем, что в выборке оказались
худшими. При 24 часах и полугоде данных пять худших часов найдутся всегда —
даже если время не значит ничего вовсе. Это ровно тот способ рассуждения,
которым были получены девять кругов подгонки в SMC.

Проверка идёт на других данных: два периода, включая медвежий 2022-23,
которого в том замере не было.

Меряется:
    1. фибо с фильтром и без — на обоих периодах
    2. SMC с киллзоной как жёстким гейтом и без — на исправленном ядре
    3. прямая разбивка результата по часам суток для обеих стратегий:
       есть ли эффект времени вообще, или это шум
    4. контроль: случайные пять часов вместо выбранных. Если запрет любых
       пяти часов даёт похожий эффект, значит дело не в этих часах.

Запуск:
    python research/sessions_test.py
"""

import os
import sys
from copy import deepcopy

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


def diff_ci(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return np.percentile(d, [2.5, 97.5]), float((d > 0).mean())


def portfolio(period, orders, max_same_direction):
    bt = period['bt']
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        max_same_direction=max_same_direction)
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        entry = pd.Timestamp(t['entry_time'])
        rows.append({'r': t['pnl'] / t['risk'], 'hour': int(entry.hour),
                     'entry_time': entry})
    stats['rows'] = pd.DataFrame(rows)
    return stats


# ── ФИБО ─────────────────────────────────────────────────────────────────────

def fibo_run(period, blocked_hours):
    import config
    from backtest_smc import fibo_orders
    original = config.BLOCK_ENTRY_HOURS_UTC
    config.BLOCK_ENTRY_HOURS_UTC = frozenset(blocked_hours)
    try:
        orders = []
        for pair, data in period['data'].items():
            orders += fibo_orders(pair, data)
        return portfolio(period, orders, config.MAX_SAME_DIRECTION)
    finally:
        config.BLOCK_ENTRY_HOURS_UTC = original


# ── SMC ──────────────────────────────────────────────────────────────────────

def smc_run(period, gate, weight):
    from smc import params as P
    from smc_sweep import build_orders
    old_gate, old_w = P.KILLZONE_AS_GATE, deepcopy(P.CONFLUENCE_WEIGHTS)
    P.KILLZONE_AS_GATE = gate
    P.CONFLUENCE_WEIGHTS = dict(P.CONFLUENCE_WEIGHTS, killzone=weight)
    try:
        orders = []
        for pair in period['data']:
            orders += build_orders(period['contexts'][pair], pair,
                                   period['data'][pair]['1h'])
        return portfolio(period, orders, P.MAX_SAME_DIRECTION)
    finally:
        P.KILLZONE_AS_GATE = old_gate
        P.CONFLUENCE_WEIGHTS = old_w


def table(title, rows):
    print()
    print('=' * 96)
    print(title)
    print('=' * 96)
    head = (f'{"конфигурация":<30}{"период":<20}{"сделок":>8}{"R/сделку":>10}'
            f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}')
    print(head)
    print('-' * len(head))
    for name, label, stats in rows:
        if stats is None:
            continue
        df = stats['rows']
        print(f'{name:<30}{label:<20}{len(df):>8}{df.r.mean():>10.3f}'
              f'{df.r.sum():>9.1f}{stats["return_pct"]:>+9.1f}'
              f'{stats["max_dd_pct"]:>7.1f}')


def by_hour(title, frames):
    """Средний R по часу входа. Показывает, есть ли эффект времени вообще."""
    print()
    print('=' * 96)
    print(title)
    print('=' * 96)
    merged = pd.concat(frames, ignore_index=True)
    print(f'{"час UTC":>8}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}'
          f'{"интервал среднего":>26}   ')
    print('-' * 80)
    for hour in range(24):
        sub = merged[merged.hour == hour]
        if len(sub) < 10:
            continue
        lo, hi = ci(sub.r)
        mark = '  <-- в блок-листе фибо' if hour in (12, 13, 14, 15, 16) else ''
        bar = '+' if sub.r.mean() > 0 else '-'
        print(f'{hour:>8}{len(sub):>8}{sub.r.mean():>10.3f}{sub.r.sum():>9.1f}'
              f'{f"[{lo:+.3f}; {hi:+.3f}]":>26}   {bar * min(int(abs(sub.r.sum())), 20)}{mark}')
    ok = sum(1 for h in range(24)
             if len(merged[merged.hour == h]) >= 10
             and merged[merged.hour == h].r.mean() > 0)
    total = sum(1 for h in range(24) if len(merged[merged.hour == h]) >= 10)
    print(f'   прибыльных часов: {ok} из {total}')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]

    # ── ФИБО ────────────────────────────────────────────────────────────────
    rows, fibo_free = [], {}
    for period in periods:
        with_filter = fibo_run(period, {12, 13, 14, 15, 16})
        without = fibo_run(period, set())
        fibo_free[period['label']] = without
        rows.append(('фибо: фильтр 12-16 UTC', period['label'], with_filter))
        rows.append(('фибо: БЕЗ фильтра', period['label'], without))
        print(f'   [{period["label"]}] фибо посчитан', flush=True)
        if with_filter and without:
            (lo, hi), p = diff_ci(without['rows'].r, with_filter['rows'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            rows.append((f'   разница без/с: ΔR '
                         f'{without["rows"].r.mean() - with_filter["rows"].r.mean():+.3f} '
                         f'[{lo:+.3f}; {hi:+.3f}] P={p:.0%} -> {verdict}',
                         '', None))
            print(f'      разница без фильтра: ΔR '
                  f'{without["rows"].r.mean() - with_filter["rows"].r.mean():+.3f} '
                  f'[{lo:+.3f}; {hi:+.3f}] -> {verdict}', flush=True)

    # Контроль: случайные пять часов
    control = []
    for period in periods:
        for seed in range(3):
            hours = set(np.random.default_rng(seed).choice(24, 5, replace=False).tolist())
            stats = fibo_run(period, hours)
            if stats:
                control.append((f'фибо: случайные {sorted(hours)}',
                                period['label'], stats))
        print(f'   [{period["label"]}] контроль посчитан', flush=True)

    table('ФИБО: НУЖЕН ЛИ БЛОК ЧАСОВ 12-16 UTC', rows)
    table('КОНТРОЛЬ: ЗАПРЕТ СЛУЧАЙНЫХ ПЯТИ ЧАСОВ', control)

    # ── SMC ─────────────────────────────────────────────────────────────────
    rows, smc_free = [], {}
    for period in periods:
        base = smc_run(period, gate=False, weight=0.0)
        gated = smc_run(period, gate=True, weight=0.0)
        weighted = smc_run(period, gate=False, weight=0.6)
        smc_free[period['label']] = base
        rows.append(('SMC: как сейчас (без фильтра)', period['label'], base))
        rows.append(('SMC: killzone жёсткий гейт', period['label'], gated))
        rows.append(('SMC: killzone весом 0.6', period['label'], weighted))
        print(f'   [{period["label"]}] SMC посчитан', flush=True)
        if base and gated:
            (lo, hi), p = diff_ci(gated['rows'].r, base['rows'].r)
            verdict = 'ЕСТЬ разница' if lo > 0 or hi < 0 else 'шум'
            print(f'      гейт против без него: ΔR '
                  f'{gated["rows"].r.mean() - base["rows"].r.mean():+.3f} '
                  f'[{lo:+.3f}; {hi:+.3f}] -> {verdict}', flush=True)

    table('SMC: НУЖНА ЛИ KILLZONE', rows)

    by_hour('ФИБО БЕЗ ФИЛЬТРА: РЕЗУЛЬТАТ ПО ЧАСУ ВХОДА (оба периода)',
            [s['rows'] for s in fibo_free.values() if s])
    by_hour('SMC: РЕЗУЛЬТАТ ПО ЧАСУ ВХОДА (оба периода)',
            [s['rows'] for s in smc_free.values() if s])

    print()
    print('ЧТЕНИЕ. Если у большинства часов интервал среднего пересекает ноль')
    print('и знаки распределены случайно — эффекта времени нет, а блок-лист')
    print('фиксирует шум конкретной выборки. Контроль на случайных часах')
    print('показывает, сколько «улучшения» даёт запрет ЛЮБЫХ пяти часов.')


if __name__ == '__main__':
    main()
