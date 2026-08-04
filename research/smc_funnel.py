"""
Где теряются сетапы: воронка отсева на реальной истории.

Улучшать «количество и качество» вслепую — это то, чем закончились шесть из
семи прежних попыток. Сначала надо увидеть, на каком шаге отсеивается
основная масса свечей, и только потом решать, что трогать.

Считается по каждой свече рабочего ТФ: причина, по которой сетап не
состоялся. Отдельно — сколько сетапов родилось, сколько дошло до налива и
сколько погибло на ограничениях портфеля (слоты, кулдаун, кэп направления).

Запуск:
    python research/smc_funnel.py
"""

import os
import sys
from collections import Counter

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, load_period)


def normalise(reason):
    """Причины с числами внутри сводим к категории, иначе строк будет тысячи."""
    if reason is None:
        return 'СИГНАЛ'
    for needle, label in (
        ('bias', 'нет направления старшего ТФ'),
        ('нога', 'импульс против направления / не той длины'),
        ('нет активных POI', 'нет свежих зон интереса'),
        ('цена уже прошла зону', 'цена уже прошла зону'),
        ('дисконт', 'зона вне дисконта/премиума'),
        ('confluence', 'подтверждений меньше порога'),
        ('RR', 'RR ниже порога'),
        ('инвалидирован', 'сетап отменён ценой'),
        ('OTE', 'зона вне OTE'),
        ('killzone', 'вне сессии'),
        ('мало данных', 'мало истории'),
        ('геометрия', 'геометрия не собралась'),
    ):
        if needle in reason:
            return label
    return reason[:44]


def walk(period):
    """Проходит все свечи всех пар и собирает причины отказа."""
    reasons = Counter()
    bars = 0
    for pair, ctx in period['contexts'].items():
        df = period['data'][pair]['1h']
        for i in range(60, len(df)):
            setup, why = ctx.evaluate(i)
            reasons[normalise(why if setup is None else None)] += 1
            bars += 1
    return reasons, bars


def portfolio_losses(period):
    """Сколько ордеров погибло на ограничениях портфеля, а не на сетапе."""
    from smc import params as P
    from smc_sweep import build_orders
    from smc_engine import run_portfolio

    bt = period['bt']
    pairs = list(period['data'])
    orders = []
    for pair in pairs:
        orders += build_orders(period['contexts'][pair], pair,
                               period['data'][pair]['1h'])
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in pairs},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        breakeven_after_tp1=P.BREAKEVEN_AFTER_TP1,
        max_hold_hours=P.MAX_POSITION_HOLD_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION)
    return len(orders), result['skipped'], result['trades']


def main():
    for cache, pairs, label in ((BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
                                (BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23')):
        period = load_period(cache, pairs, label)

        reasons, bars = walk(period)
        print()
        print('=' * 88)
        print(f'{label.upper()}: почему свеча не даёт сетапа')
        print('=' * 88)
        print(f'{"причина":<44}{"свечей":>10}{"доля":>9}')
        print('-' * 63)
        for name, count in reasons.most_common():
            print(f'{name:<44}{count:>10}{count / bars * 100:>8.1f}%')
        print('-' * 63)
        print(f'{"ВСЕГО свечей":<44}{bars:>10}')

        n_orders, skipped, trades = portfolio_losses(period)
        print()
        print(f'Сетапов (после дедупликации по зоне): {n_orders}')
        print(f'Из них потеряно ДО сделки:')
        titles = {
            'duplicate': 'та же зона уже отработана',
            'active': 'по паре уже открыта позиция',
            'cooldown': 'кулдаун после недавней сделки',
            'capacity': 'все слоты заняты',
            'same_direction': 'кэп по направлению',
            'no_fill': 'лимит не налился за срок жизни',
        }
        for key, count in skipped.items():
            if count:
                print(f'   {titles.get(key, key):<40}{count:>7}'
                      f'{count / max(n_orders, 1) * 100:>7.1f}%')
        print(f'   {"состоялось сделок":<40}{len(trades):>7}'
              f'{len(trades) / max(n_orders, 1) * 100:>7.1f}%')

        if trades:
            r = np.array([t['pnl'] / t['risk'] for t in trades if t['risk']])
            print(f'\nСредний R: {r.mean():+.3f}   сумма R: {r.sum():+.1f}   '
                  f'винрейт: {(r > 0).mean() * 100:.1f}%')


if __name__ == '__main__':
    main()
