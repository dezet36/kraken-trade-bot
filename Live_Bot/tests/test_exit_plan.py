"""
Тесты плана выхода.

Регрессия, ради которой они написаны: план частичной фиксации брался из
глобального `config.TP_CLOSE_FRACTIONS`, откалиброванного под фибо-стратегию
(одна цель, 100% позиции). SMC рассчитывает три цели с долями 25/25/50, и её
план молча подменялся чужим — позиция закрывалась целиком на первой цели.
По бэктесту это ровно та конфигурация, которая уходит в минус (−9.1% против
+40.6% на трёх целях). Ошибка была тихой: логи и журнал выглядели нормально,
просто стратегия торговала не то, что проверялось.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exit_plan import (direction_cap, tp_plan, tps_completed,   # noqa: E402
                       wants_breakeven)


class TestPlanFromSignal:
    def test_strategy_plan_wins_over_globals(self):
        targets, fractions = tp_plan({
            'tp_targets': [110.0, 115.0, 120.0],
            'tp_fractions': [0.25, 0.25, 0.50],
            'take_profit_1': 110.0, 'take_profit_2': 115.0,
        })

        assert targets == [110.0, 115.0, 120.0]
        assert fractions == [0.25, 0.25, 0.50]

    def test_fractions_always_sum_to_one(self):
        """
        Недобор долей оставил бы часть позиции висеть после последней цели,
        перебор — закрыл бы её дважды.
        """
        _targets, fractions = tp_plan({
            'tp_targets': [110.0, 115.0, 120.0],
            'tp_fractions': [0.3, 0.3, 0.3],
        })

        assert sum(fractions) == pytest.approx(1.0)
        assert fractions[-1] == pytest.approx(0.4)

    def test_extra_targets_without_fractions_are_dropped(self):
        """Цель без своей доли исполнить нечем — она не должна попасть в план."""
        targets, fractions = tp_plan({
            'tp_targets': [110.0, 115.0, 120.0],
            'tp_fractions': [0.5, 0.5],
        })

        assert targets == [110.0, 115.0]
        assert len(fractions) == 2


class TestLegacyFallback:
    def test_signal_without_plan_falls_back_to_globals(self):
        """
        Позиции и pending-ордера, сохранённые прошлой версией бота, плана не
        содержат. Они должны доработаться по-старому, а не остаться без целей.
        """
        targets, fractions = tp_plan({'take_profit_1': 130.0, 'take_profit_2': 131.0})

        assert targets == [130.0]
        assert fractions == [1.0]

    def test_no_targets_at_all_is_empty_plan(self):
        assert tp_plan({}) == ([], [])


class TestRestartRecovery:
    """
    После рестарта бот видит на бирже уменьшившийся размер позиции и должен
    понять, с какой цели продолжать. Прежняя формула делила закрытую часть на
    фиксированные 0.5 — на плане 25/25/50 это давало неверный ответ, и остаток
    позиции закрывался по чужому уровню.
    """

    PLAN = [0.25, 0.25, 0.50]

    def test_nothing_closed_yet(self):
        assert tps_completed(10.0, 10.0, self.PLAN) == 0

    def test_first_target_taken(self):
        assert tps_completed(7.5, 10.0, self.PLAN) == 1

    def test_second_target_taken(self):
        assert tps_completed(5.0, 10.0, self.PLAN) == 2

    def test_old_formula_would_have_said_zero_here(self):
        """Закрыто 25% — деление на 0.5 округляло это в ноль целей."""
        assert round((1.0 - 7.5 / 10.0) / 0.5) == 0     # как считалось раньше
        assert tps_completed(7.5, 10.0, self.PLAN) == 1  # как считается теперь

    def test_rounding_of_exchange_size_tolerated(self):
        """Биржа округляет размер контракта — точного равенства долей не будет."""
        assert tps_completed(7.4999, 10.0, self.PLAN) == 1

    def test_single_target_plan_never_reports_partial(self):
        assert tps_completed(10.0, 10.0, [1.0]) == 0

    def test_impossible_sizes_are_safe(self):
        assert tps_completed(12.0, 10.0, self.PLAN) == 0
        assert tps_completed(5.0, 0.0, self.PLAN) == 0


class TestBreakeven:
    def test_strategy_can_switch_breakeven_off(self):
        assert wants_breakeven({'breakeven_after_tp': False}) is False

    def test_default_keeps_fibo_behaviour(self):
        """Сигнал без явного решения ведёт себя как раньше — безубыток включён."""
        assert wants_breakeven({}) is True


class TestDirectionCap:
    """
    Ограничение на число позиций в одну сторону — тоже настройка стратегии.
    Фибо-модель считалась без него вовсе, у SMC при винрейте около 25%
    коррелированные лонги складываются в глубокую просадку.
    """

    def test_signal_cap_wins_over_global(self):
        assert direction_cap({'max_same_direction': 2}) == 2

    def test_zero_means_no_limit(self):
        assert direction_cap({'max_same_direction': 0}) == 0

    def test_missing_falls_back_to_global(self):
        assert direction_cap({}) == 0        # config.MAX_SAME_DIRECTION по умолчанию

    def test_broken_value_does_not_crash_entry(self):
        """Мусор в настройке не должен ронять вход — он лишь снимает лимит."""
        assert direction_cap({'max_same_direction': 'два'}) == 0
        assert direction_cap({'max_same_direction': -5}) == 0
