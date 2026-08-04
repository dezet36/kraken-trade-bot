"""
Диагностика SMC: где именно теряются сделки.

Главный инструмент — MFE (maximum favorable excursion): насколько далеко цена
успевала пройти В ПОЛЬЗУ позиции перед тем, как её выбило стопом. Если
убыточные сделки регулярно доходят до 1R, значит проблема не во входах, а в
слишком далёкой первой цели: эти движения можно было зафиксировать.

Симметрично MAE (maximum adverse excursion) по прибыльным сделкам показывает,
насколько близко стоп был к срабатыванию — то есть можно ли его подтянуть.

Запуск:
    python research/smc_diagnose.py
    python research/smc_diagnose.py --pairs BTCUSDT,ETHUSDT --risk-free
"""

import argparse
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc import params as P, signal as smc_signal  # noqa: E402
from smc_engine import compute_stats, run_portfolio  # noqa: E402
from backtest_smc import COOLDOWN_HOURS, MAX_POSITIONS, RISK_PCT, load_pair  # noqa: E402
from smc_sweep import build_orders  # noqa: E402

DIAG_PAIRS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'LINKUSDT',
              'AVAXUSDT', 'DOGEUSDT', 'ADAUSDT', 'BNBUSDT', 'LTCUSDT']


def histogram(values, edges, label):
    """Доля значений, ДОСТИГШИХ каждого порога."""
    total = len(values)
    if not total:
        print(f'   {label}: нет данных')
        return
    arr = np.asarray(values, dtype=float)
    print(f'   {label} (n={total}):')
    for edge in edges:
        share = float((arr >= edge).mean()) * 100
        bar = '█' * int(share / 2)
        print(f'      достигли {edge:>4.1f}R: {share:5.1f}%  {bar}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pairs', default=','.join(DIAG_PAIRS))
    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(',') if p.strip()]

    print(f'Загрузка {len(pairs)} пар...', flush=True)
    data = {}
    for pair in pairs:
        loaded = load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    if not data:
        print('Нет данных.')
        return

    print('Построение контекстов...', flush=True)
    orders = []
    for pair, frames in data.items():
        ctx = smc_signal.build_context(
            {'bias': frames['1d'], 'htf': frames['4h'], 'poi': frames['1h']}, pair=pair)
        found = build_orders(ctx, pair, frames['1h'])
        orders += found
        print(f'   {pair}: {len(found)} сетапов', flush=True)

    exec_data = {pair: frames['5m'] for pair, frames in data.items()}
    outcome = run_portfolio(
        orders, exec_data, risk_pct=RISK_PCT, max_positions=MAX_POSITIONS,
        cooldown_hours=COOLDOWN_HOURS,
        breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
        max_hold_hours=P.MAX_POSITION_HOLD_HOURS)

    stats = compute_stats(outcome, label='SMC')
    trades = outcome['trades']
    print(f'\nСделок: {stats["trades"]}  доход {stats["return_pct"]:+.1f}%  '
          f'PF {stats["profit_factor"]:.3f}  WR {stats["winrate"]:.1f}%')
    print(f'Ордеров: {len(orders)}, не налилось: {outcome["skipped"]["no_fill"]} '
          f'({outcome["skipped"]["no_fill"] / max(len(orders), 1) * 100:.0f}%)')

    losers = [t for t in trades if t['pnl'] <= 0]
    winners = [t for t in trades if t['pnl'] > 0]

    print('\n' + '=' * 66)
    print('СКОЛЬКО УБЫТОЧНЫЕ СДЕЛКИ УСПЕВАЛИ ПРОЙТИ В НАШУ ПОЛЬЗУ')
    print('=' * 66)
    print('Если заметная доля доходит до 1R и выше — эти движения теряются')
    print('из-за слишком далёкой первой цели, а не из-за плохих входов.\n')
    histogram([t['mfe_r'] for t in losers], [0.5, 1.0, 1.5, 2.0, 3.0], 'MFE убыточных')

    print('\n' + '=' * 66)
    print('НАСКОЛЬКО БЛИЗКО СТОП ПОДХОДИЛ К СРАБАТЫВАНИЮ У ПРИБЫЛЬНЫХ')
    print('=' * 66)
    print('Высокая доля near-1.0R означает, что стоп впритык: любое ужатие')
    print('убьёт часть победителей.\n')
    histogram([t['mae_r'] for t in winners], [0.25, 0.5, 0.75, 0.9], 'MAE прибыльных')

    print('\n' + '=' * 66)
    print('РАССТОЯНИЕ ДО ПЕРВОЙ ЦЕЛИ')
    print('=' * 66)
    rr_first = [o.meta.get('rr') for o in orders if o.meta.get('rr')]
    if rr_first:
        series = pd.Series(rr_first)
        print(f'   взвешенный RR сетапов: медиана {series.median():.2f}, '
              f'p25 {series.quantile(.25):.2f}, p75 {series.quantile(.75):.2f}')

    reasons = Counter(t['exit_reason'] for t in trades)
    print(f'\n   причины выхода: {dict(reasons)}')

    reached_any = sum(1 for t in trades if t['tps_hit'] > 0)
    print(f'   дошли хотя бы до первой цели: {reached_any}/{len(trades)} '
          f'({reached_any / max(len(trades), 1) * 100:.0f}%)')

    # Потенциал закрытия части позиции на фиксированном R
    print('\n' + '=' * 66)
    print('ОЦЕНКА: ЧТО ДАЛА БЫ ФИКСАЦИЯ ЧАСТИ ПОЗИЦИИ НА БЛИЗКОЙ ЦЕЛИ')
    print('=' * 66)
    print('Грубая прикидка по MFE: сколько R добавила бы продажа 1/3 позиции')
    print('на уровне X R у сделок, которые до него дошли.\n')
    for level in (0.75, 1.0, 1.25, 1.5, 2.0):
        gain = sum(level / 3 for t in trades if t['mfe_r'] >= level)
        # у тех, кто дошёл, оставшиеся 2/3 идут по факту -> вычитаем то,
        # что эта треть заработала бы по фактическому исходу
        actual = sum((t['pnl'] / t['risk']) / 3 for t in trades if t['mfe_r'] >= level)
        delta = gain - actual
        print(f'   фиксация 1/3 на {level:.2f}R: изменение суммы R '
              f'{delta:+.1f} (было {stats["sum_r"]:.1f})')


if __name__ == '__main__':
    main()
