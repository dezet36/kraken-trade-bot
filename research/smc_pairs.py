"""
Круг 17: масштабируется ли количество сделок числом пар.

Все пороги проверены и ни один не даёт количества. Воронка объясняет почему:
42-50% ордеров не наливаются, а если заставить их наливаться чаще (жизнь
ордера 120-240ч), число сделок ПАДАЕТ — налившийся ордер занимает пару и
запускает кулдаун, вытесняя более свежие сетапы. Конвейер насыщен: внутри
одной пары место занято почти всегда.

Отсюда единственный оставшийся рычаг — не пороги, а число независимых
потоков. Здесь это проверяется прямо: одна и та же конфигурация гоняется на
5, 10, 15 и всех парах периода.

Что именно проверяется:

  - сделок на пару — если оно держится, количество линейно по числу пар;
    если падает, пары мешают друг другу через общие слоты и лимит
    направления, и расширение пула упрётся в потолок;
  - средний R — добавляемые пары не должны быть хуже: рост количества за
    счёт мусорных инструментов не нужен.

Пары берутся в порядке ликвидности (как их отдаёт список периода), поэтому
подмножество из 5 — это самые ликвидные, а не случайные.

Запуск:
    python research/smc_pairs.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, ci, load_period)

SIZES = (5, 10, 15, None)      # None — все пары периода

RNG = np.random.default_rng(20260805)


def build_all(period):
    from smc_sweep import build_orders
    by_pair = {}
    for pair in period['data']:
        by_pair[pair] = build_orders(period['contexts'][pair], pair,
                                     period['data'][pair]['1h'])
    return by_pair


def run_subset(period, by_pair, pairs):
    from smc import params as P
    from smc_engine import compute_stats, run_portfolio
    bt = period['bt']
    orders = [o for p in pairs for o in by_pair[p]]
    if not orders:
        return None
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in pairs},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION)
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = [{'r': t['pnl'] / t['risk']} for t in result['trades'] if t.get('risk')]
    stats['rows'] = pd.DataFrame(rows)
    stats['orders'] = len(orders)
    return stats


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]

    for period in periods:
        by_pair = build_all(period)
        available = list(period['data'])
        print()
        print('=' * 104)
        print(f'{period["label"].upper()}  (доступно пар: {len(available)})')
        print('=' * 104)
        head = (f'{"пар":>5}{"ордеров":>10}{"сделок":>9}{"сделок/пару":>14}'
                f'{"R/сделку":>10}{"сумма R":>9}{"доход%":>9}{"DD%":>7}'
                f'{"не налилось":>13}')
        print(head)
        print('-' * len(head))
        for size in SIZES:
            pairs = available if size is None else available[:size]
            if size is not None and len(pairs) < size:
                continue
            stats = run_subset(period, by_pair, pairs)
            if stats is None:
                continue
            df = stats['rows']
            lo, hi = ci(df.r)
            no_fill = (stats.get('skipped') or {}).get('no_fill', 0)
            print(f'{len(pairs):>5}{stats["orders"]:>10}{len(df):>9}'
                  f'{len(df) / len(pairs):>14.1f}{df.r.mean():>10.3f}'
                  f'{df.r.sum():>9.1f}{stats["return_pct"]:>+9.1f}'
                  f'{stats["max_dd_pct"]:>7.1f}{no_fill:>13}'
                  f'   [{lo:+.2f}; {hi:+.2f}]')

    print()
    print('Читается по колонке «сделок/пару». Держится — количество линейно по')
    print('числу пар, и расширение пула это единственный работающий рычаг.')
    print('Падает — пары конкурируют за общие слоты и лимит направления, и')
    print('добавлять их бессмысленно без снятия портфельных ограничений.')


if __name__ == '__main__':
    main()
