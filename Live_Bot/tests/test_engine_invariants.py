"""
Инварианты движка исполнения — на сценариях с ЗАРАНЕЕ ИЗВЕСТНЫМ ответом.

Движок бэктеста определяет все выводы проекта: если он ошибается в свою
пользу, подбор параметров будет оптимизировать несуществующее преимущество.
Проверить его на реальных данных нельзя — там неизвестен правильный ответ.
Поэтому здесь строятся свечи, где ответ считается на бумаге.

Каждый тест закрывает конкретный способ ошибиться в свою пользу.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'research'))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Движок бэктеста живёт в research/ и в серверную сборку не входит: боту он
# в работе не нужен. Пропуск вместо падения — иначе проверка после
# обновления на сервере валилась бы всегда, и автоматический откат
# срабатывал бы на каждом обновлении, каким бы исправным оно ни было.
eng = pytest.importorskip(
    'smc_engine',
    reason='движок бэктеста (research/) отсутствует — это серверная сборка')

T0 = pd.Timestamp('2026-01-01', tz='UTC')


def bars(rows):
    """rows: (high, low, close). Открытие для модели значения не имеет."""
    return pd.DataFrame({
        'timestamp': [T0 + pd.Timedelta(minutes=5 * i) for i in range(len(rows))],
        'open': [r[2] for r in rows],
        'high': [r[0] for r in rows],
        'low': [r[1] for r in rows],
        'close': [r[2] for r in rows],
    })


def order(entry=100.0, stop=90.0, targets=(130.0,), fractions=(1.0,),
          direction='BULLISH', hours=72):
    return eng.Order(
        pair='TEST', direction=direction, entry=entry, stop=stop,
        targets=list(targets), fractions=list(fractions),
        created=np.datetime64(T0.tz_localize(None)),
        expires=np.datetime64(T0.tz_localize(None)) + np.timedelta64(hours * 3600, 's'),
        key='k',
    )


def run(rows, o=None, risk=100.0, **kw):
    o = o or order()
    return eng.simulate_order(o, eng._prepare(bars(rows)), 0, risk, **kw)


class TestFill:
    def test_no_fill_when_price_never_reaches_limit(self):
        """Лимит, до которого рынок не дошёл, сделкой не становится."""
        assert run([(105, 101, 103), (106, 102, 104)]) is None

    def test_fill_at_limit_price_not_at_close(self):
        """
        Лимитный ордер исполняется по СВОЕЙ цене. Брать цену закрытия свечи
        значило бы приписывать себе движение, которого не было.
        """
        result = run([(105, 99, 104), (135, 100, 134)])

        assert result is not None
        assert result['entry'] == 100.0

    def test_expired_order_is_not_a_trade(self):
        rows = [(105, 101, 103)] * 20 + [(105, 99, 104)]
        o = order(hours=1)      # истечёт через 12 пятиминуток

        assert run(rows, o) is None


class TestIntrabarConflict:
    def test_stop_wins_when_both_touched_in_one_bar(self):
        """
        Свеча задела и стоп, и цель. Порядок внутри свечи неизвестен, и выбор
        в свою пользу — самый частый способ нарисовать несуществующий доход.
        """
        result = run([(100, 100, 100), (135, 85, 120)])

        assert result['exit_reason'] == 'SL'
        assert result['pnl'] < 0

    def test_stop_on_the_fill_bar_is_counted(self):
        """
        Регрессия: свеча налива не проверялась на стоп, и сделка, которую та
        же свеча уносила в минус, получала бесплатный шанс. На реальных
        данных это 2.4% сделок ценой около 1R каждая.
        """
        result = run([(105, 85, 95), (135, 100, 134)])

        assert result is not None
        assert result['exit_reason'] == 'SL'


class TestCosts:
    def test_fees_and_funding_reduce_result(self):
        result = run([(100, 100, 100), (131, 99, 130)])

        assert result['fees'] > 0
        assert result['pnl'] < result['gross_pnl']
        assert result['pnl'] == pytest.approx(
            result['gross_pnl'] - result['fees'] - result['funding'])

    def test_stop_exit_slips_against_position(self):
        """Стоп-маркет срабатывает на движении — цена уходит дальше уровня."""
        result = run([(100, 100, 100), (101, 85, 88)])

        assert result['exit'] < 90.0

    def test_target_exit_does_not_slip(self):
        """Лимитный тейк исполняется по своей цене — проскальзывать нечему."""
        result = run([(100, 100, 100), (131, 99, 130)])

        assert result['exit'] == 130.0


class TestPartialTargets:
    def test_partial_fixation_then_stop_is_not_a_full_loss(self):
        """
        Взята первая цель из трёх, потом стоп. Четверть позиции уже в плюсе,
        и считать это полным минусом — исказить статистику стратегии.
        """
        o = order(targets=(110.0, 120.0, 140.0), fractions=(0.25, 0.25, 0.5))
        result = run([(100, 100, 100), (111, 99, 110), (101, 85, 88)], o,
                     breakeven_after_tp1=False)

        assert result['tps_hit'] == 1
        assert result['exit_reason'] == 'SL_after_TP1'

    def test_all_targets_taken_closes_position(self):
        o = order(targets=(110.0, 120.0, 140.0), fractions=(0.25, 0.25, 0.5))
        result = run([(100, 100, 100), (111, 99, 110), (121, 109, 120),
                      (141, 119, 140)], o, breakeven_after_tp1=False)

        assert result['tps_hit'] == 3
        assert result['exit_reason'] == 'TP3'
        # 0.25*1R + 0.25*2R + 0.5*4R = 2.75R до издержек
        assert result['gross_pnl'] / 100.0 == pytest.approx(2.75, abs=0.01)

    def test_breakeven_after_first_target_can_be_switched_off(self):
        """
        У SMC безубыток выключен намеренно: он выбивает позицию шумом до
        дальних целей. Движок обязан уметь его не применять.
        """
        rows = [(100, 100, 100), (111, 99, 110), (105, 99.5, 100), (141, 104, 140)]
        o = order(targets=(110.0, 120.0, 140.0), fractions=(0.25, 0.25, 0.5))

        with_be = run(rows, o, breakeven_after_tp1=True)
        without = run(rows, o, breakeven_after_tp1=False)

        assert with_be['exit_reason'] == 'SL_after_TP1'
        assert without['tps_hit'] == 3


class TestShort:
    def test_short_mirrors_long(self):
        o = order(entry=100.0, stop=110.0, targets=(70.0,), direction='BEARISH')
        result = run([(100, 100, 100), (101, 69, 70)], o)

        assert result['exit_reason'] == 'TP1'
        assert result['gross_pnl'] > 0

    def test_short_stop_is_above_entry(self):
        o = order(entry=100.0, stop=110.0, targets=(70.0,), direction='BEARISH')
        result = run([(100, 100, 100), (112, 99, 111)], o)

        assert result['exit_reason'] == 'SL'
        assert result['exit'] > 110.0        # проскальзывание против шорта


class TestTimeStop:
    def test_position_closed_after_deadline(self):
        rows = [(100, 100, 100)] + [(105, 99, 102)] * 30
        result = run(rows, max_hold_hours=1.0)

        assert result['exit_reason'] == 'TIME_STOP'
