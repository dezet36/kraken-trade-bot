"""
Сигнал старшей свечи не должен исполняться внутри неё самой.

ОТКУДА ЭТОТ ТЕСТ. Замер параметров Боллинджера на четырёхчасовых свечах дал
+0.610 R на сделку — больше, чем у любой принятой стратегии проекта. Причина
оказалась не в стратегии:

    отметка свечи — это её ОТКРЫТИЕ, а сигнал считается по ЗАКРЫТИЮ;
    движок начинает исполнение с бара, следующего за created.

При часовом сигнале и часовом исполнении это совпадает само собой. При
четырёхчасовом сигнале и часовом исполнении заявка начинала наливаться через
час после открытия сигнальной свечи — то есть ВНУТРИ неё, по цене, о которой
станет известно только через три часа.

Ошибка не падает и не выглядит подозрительно нигде, кроме итогового числа.
Поэтому проверка стоит на самом движке: сколько бы ни менялись стратегии,
заявка, созданная по закрытию свечи, не может налиться внутри этой свечи.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), 'research'))


def hourly(n=12, low_at=None, low_price=90.0):
    """Часовые свечи ровно по 100, с провалом в одном заданном баре."""
    frame = pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=n, freq='h'),
        'open': 100.0, 'high': 100.5, 'low': 99.5, 'close': 100.0,
        'volume': 1.0,
    })
    if low_at is not None:
        frame.loc[low_at, 'low'] = low_price
    return frame


def an_order(created, entry=95.0):
    from smc_engine import Order
    return Order(
        pair='BTCUSDT', direction='LONG', entry=entry, stop=entry - 5,
        targets=[entry + 5], fractions=[1.0],
        created=np.datetime64(pd.Timestamp(created)),
        expires=np.datetime64(pd.Timestamp(created) + pd.Timedelta(hours=8)),
        key=('BTCUSDT', 1), entry_type='limit')


def fills(order, frame):
    from smc_engine import run_portfolio
    result = run_portfolio([order], {'BTCUSDT': frame}, risk_pct=1.0,
                           max_positions=5, cooldown_hours=0.0,
                           max_hold_hours=48)
    return [t for t in result['trades'] if t.get('risk')]


class TestExecutionStartsAfterSignal:
    def test_bar_of_the_signal_itself_is_not_tradable(self):
        """
        Заявка, созданная по закрытию бара 3, не может налиться на баре 3.

        Провал цены сидит ровно в том баре, чьё закрытие и породило сигнал.
        Если движок его засчитает — значит он торгует по информации, которой
        на момент решения не было.
        """
        frame = hourly(low_at=3)
        created = frame['timestamp'].iloc[3]        # отметка = ОТКРЫТИЕ бара 3
        assert fills(an_order(created), frame) == []

    def test_next_bar_is_tradable(self):
        """Контроль: на следующем баре та же заявка обязана налиться."""
        frame = hourly(low_at=4)
        created = frame['timestamp'].iloc[3]
        trades = fills(an_order(created), frame)
        assert len(trades) == 1
        assert trades[0]['entry'] == pytest.approx(95.0)


class TestHigherTimeframeSignal:
    """
    Четырёхчасовой сигнал на часовом исполнении — тот самый случай.

    Сигнальная свеча покрывает часы 0-3. Её отметка — час 0. Заявка,
    созданная по её закрытию, не может налиться ни в одном из часов 0-3.
    """

    def test_naive_created_time_leaks_the_future(self):
        frame = hourly(n=12, low_at=2)             # провал ВНУТРИ свечи 0-3
        naive = frame['timestamp'].iloc[0]         # отметка старшей свечи
        leaked = fills(an_order(naive), frame)
        assert leaked, 'без сдвига заявка наливается внутри своей же свечи'

        # Правильное время создания — последний часовой бар внутри старшей.
        shifted = frame['timestamp'].iloc[0] + pd.Timedelta(hours=3)
        assert fills(an_order(shifted), frame) == []

    def test_shift_still_allows_the_next_candle(self):
        frame = hourly(n=12, low_at=5)             # провал уже в следующей
        shifted = frame['timestamp'].iloc[0] + pd.Timedelta(hours=3)
        trades = fills(an_order(shifted), frame)
        assert len(trades) == 1
