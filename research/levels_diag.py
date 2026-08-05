"""
Диагностика стратегии уровней на БОЕВОМ ядре: где именно она теряет.

Правило то же, что и в разборе SMC: сначала измерить, потом чинить.
Догадка «наверное, надо подтянуть стоп» уже дважды за эту сессию
оказывалась дороже, чем замер.

Что известно из прогона на исправленном ядре:

    бык     696 сделок  винрейт 41%  +0.120 R  просадка 16.5%
    медведь 633 сделки  винрейт 39%  +0.150 R  просадка 14.5%

    по режимам:  падение +0.497 [+0.262; +0.742]   257 сделок
                 рост    +0.068 [-0.214; +0.374]   135 сделок
                 боковик +0.048 [-0.057; +0.154]   887 сделок

Две трети сделок приходятся на боковик, и преимущества там нет. Это
первое, что надо объяснить.

Меряется:
    1. чем заканчиваются сделки и сколько каждая категория приносит;
    2. лестница MFE: как далеко цена уходит в нашу сторону — цель близко
       или далеко;
    3. доля убыточных сделок, побывавших в плюсе (проблема входа или выхода);
    4. зависимость результата от объёма на возврате и от RR сетапа —
       предсказывают ли они хоть что-то;
    5. глубина прокола и задержка возврата — те же вопросы;
    6. помесячно и по режимам.

Запуск:
    python research/levels_diag.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from levels import core, params as LP  # noqa: E402
from levels_backtest import build_orders  # noqa: E402
from smc_engine import compute_stats, run_portfolio  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period)


def run(period):
    orders = []
    for pair, data in period['data'].items():
        orders += build_orders(pair, data['1h'])
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
        meta = t.get('meta') or {}
        rows.append({
            'r': t['pnl'] / t['risk'],
            'regime': period['regime'](t['entry_time']),
            'direction': 'LONG' if t['direction'] in ('BULLISH', 'LONG') else 'SHORT',
            'reason': str(t.get('exit_reason', '')),
            'mfe_r': float(t.get('mfe_r', 0) or 0),
            'mae_r': float(t.get('mae_r', 0) or 0),
            'volume_ratio': float(meta.get('volume_ratio', 0)),
            'rr': float(meta.get('rr', 0)),
            'touches': int(meta.get('touches', 0)),
            'entry_time': pd.Timestamp(t['entry_time']),
            'days': (pd.Timestamp(t['exit_time']) - pd.Timestamp(t['entry_time'])
                     ).total_seconds() / 86400,
        })
    stats['rows'] = pd.DataFrame(rows)
    stats['skipped_raw'] = result['skipped']
    stats['orders'] = len(orders)
    return stats


def block(title):
    print()
    print('=' * 96)
    print(title)
    print('=' * 96)


def by_exit(frames):
    block('1. ЧЕМ ЗАКАНЧИВАЮТСЯ СДЕЛКИ')
    for label, df in frames.items():
        print()
        print(label)
        grouped = df.groupby('reason').r.agg(['count', 'sum', 'mean'])
        for reason, row in grouped.sort_values('count', ascending=False).iterrows():
            print(f'   {reason:<16}{int(row["count"]):>5} сд {row["count"] / len(df) * 100:>5.0f}%  '
                  f'{row["sum"]:>+8.1f} R  {row["mean"]:>+6.2f} ср')


def mfe_ladder(frames):
    block('2. КАК ДАЛЕКО ЦЕНА УХОДИТ В НАШУ СТОРОНУ')
    steps = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
    head = f'{"период":<20}' + ''.join(f'{f">={s}R":>9}' for s in steps) + f'{"медиана":>10}'
    print(head)
    print('-' * len(head))
    for label, df in frames.items():
        print(f'{label:<20}' + ''.join(f'{(df.mfe_r >= s).mean() * 100:>8.0f}%' for s in steps)
              + f'{df.mfe_r.median():>10.2f}')
    print()
    print('Если цена регулярно уходит заметно дальше цели — цель слишком')
    print('близко, и хвост движения достаётся не нам.')


def gave_back(frames):
    block('3. СКОЛЬКО ОТДАЁТСЯ ОБРАТНО')
    head = (f'{"период":<20}{"убыточных":>11}{"из них были +1R":>18}'
            f'{"были +2R":>11}{"средний MAE":>13}')
    print(head)
    print('-' * len(head))
    for label, df in frames.items():
        loss = df[df.r <= 0]
        g1 = (loss.mfe_r >= 1).mean() * 100 if len(loss) else float('nan')
        g2 = (loss.mfe_r >= 2).mean() * 100 if len(loss) else float('nan')
        print(f'{label:<20}{len(loss):>10}{g1:>17.0f}%{g2:>10.0f}%{df.mae_r.mean():>13.2f}')
    print()
    print('Высокая доля «были в плюсе» означает, что беда в выходах, а не')
    print('во входах.')


def predictors(frames):
    block('4. ПРЕДСКАЗЫВАЮТ ЛИ ПРИЗНАКИ СЕТАПА РЕЗУЛЬТАТ')
    merged = pd.concat(frames.values(), ignore_index=True)
    for col, title, bins in (
        ('volume_ratio', 'объём на возврате', [1.5, 2, 2.5, 3, 4, 99]),
        ('rr', 'RR сетапа', [1.5, 2, 2.5, 3, 4, 99]),
        ('touches', 'касаний уровня', [2, 3, 4, 5, 99]),
    ):
        print()
        print(f'   {title}:')
        lo = bins[0]
        for hi in bins[1:]:
            sub = merged[(merged[col] >= lo) & (merged[col] < hi)]
            if len(sub) >= 25:
                a, b = ci(sub.r)
                print(f'      {lo:>4}-{hi if hi < 99 else "..":<4} {len(sub):>5} сделок  '
                      f'R/сделку {sub.r.mean():+.3f}  [{a:+.3f}; {b:+.3f}]')
            lo = hi


def timing(frames):
    block('5. ГЛУБИНА ПРОКОЛА, ЗАДЕРЖКА И ДЛИТЕЛЬНОСТЬ')
    merged = pd.concat(frames.values(), ignore_index=True)
    print(f'   медиана удержания: {merged.days.median() * 24:.1f} ч')
    print(f'   доля сделок короче часа: {(merged.days * 24 < 1).mean() * 100:.0f}%')
    print()
    for lo, hi in ((0, 0.1), (0.1, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 99)):
        sub = merged[(merged.days >= lo) & (merged.days < hi)]
        if len(sub) < 25:
            continue
        a, b = ci(sub.r)
        print(f'   удержание {lo * 24:>4.0f}-{hi * 24 if hi < 99 else 999:<4.0f} ч  '
              f'{len(sub):>5} сделок  R/сделку {sub.r.mean():+.3f}  [{a:+.3f}; {b:+.3f}]')


def by_regime_side(frames):
    block('6. РЕЖИМ И СТОРОНА')
    merged = pd.concat(frames.values(), ignore_index=True)
    head = f'{"режим":<12}{"сторона":<9}{"сделок":>8}{"R/сделку":>10}{"сумма R":>9}{"интервал":>24}'
    print(head)
    print('-' * len(head))
    for reg in REGIMES:
        for side in ('LONG', 'SHORT'):
            sub = merged[(merged.regime == reg) & (merged.direction == side)]
            if len(sub) < 20:
                continue
            a, b = ci(sub.r)
            print(f'{reg:<12}{side:<9}{len(sub):>8}{sub.r.mean():>10.3f}{sub.r.sum():>9.1f}'
                  f'{f"[{a:+.3f}; {b:+.3f}]":>24}')


def funnel(stats_by_label):
    block('7. ВОРОНКА: КУДА ДЕВАЮТСЯ ЗАЯВКИ')
    ru = {'duplicate': 'та же зона уже отработана', 'active': 'по паре уже есть позиция',
          'cooldown': 'кулдаун', 'capacity': 'нет свободного слота',
          'same_direction': 'лимит одной стороны', 'no_fill': 'вход не налился'}
    for label, stats in stats_by_label.items():
        print()
        print(f'{label}: заявок {stats["orders"]} -> сделок {len(stats["rows"])} '
              f'({len(stats["rows"]) / stats["orders"]:.0%})')
        for key, count in sorted(stats['skipped_raw'].items(), key=lambda kv: -kv[1]):
            if count:
                print(f'   {ru.get(key, key):<28}{count:>5}  {count / stats["orders"]:>4.0%}')


def monthly(frames):
    block('8. ПОМЕСЯЧНО')
    for label, df in frames.items():
        month = df.set_index('entry_time').resample('MS').r.agg(['count', 'sum'])
        month = month[month['count'] > 0]
        print()
        print(f'{label}: прибыльных {(month["sum"] > 0).mean() * 100:.0f}% из {len(month)}')
        for ts, row in month.iterrows():
            bar = ('+' if row['sum'] >= 0 else '-') * min(int(abs(row['sum'])), 40)
            print(f'   {ts:%Y-%m}  {int(row["count"]):>4} сд  {row["sum"]:>+7.1f} R  {bar}')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    frames, stats_by_label = {}, {}
    for period in periods:
        stats = run(period)
        if stats is None:
            continue
        frames[period['label']] = stats['rows']
        stats_by_label[period['label']] = stats
        print(f'   [{period["label"]}] {len(stats["rows"])} сделок', flush=True)

    by_exit(frames)
    mfe_ladder(frames)
    gave_back(frames)
    predictors(frames)
    timing(frames)
    by_regime_side(frames)
    funnel(stats_by_label)
    monthly(frames)


if __name__ == '__main__':
    main()
