"""
Дневной убыток считается за СУТКИ, а не за всё время.

ЗДЕСЬ БЫЛА ОСТАНОВКА, КОТОРАЯ НЕ ОТПУСКАЛА. Дневным убытком считался общий итог
с начала работы: bankroll − (деньги + запас). Такая мера не обнуляется никогда,
и просадка в 5% останавливала торговлю НАВСЕГДА — не до завтра, а до ручного
вмешательства.

Поймано на живом счёте:

    «дневной убыток 7.79 достиг предела 2.00»
    четырнадцать заявок не отправлено, ноль на бирже
    настоящий итог при этом +$0.84

Число выросло из разовой правки учёта, а не из торговли. Мера, которую двигает
исправление бухгалтерии, не годится в предохранители.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import engine  # noqa: E402


def _maker(tmp_path, cash=40.0):
    made = engine.PaperMaker(bankroll=40.0,
                             state_path=os.path.join(str(tmp_path), 's.json'))
    made.state['cash'] = cash
    made.state.pop('day_anchor', None)
    return made


class TestTheDayIsTheUnit:

    def test_a_fresh_day_starts_at_zero(self, tmp_path):
        """Каким бы ни был счёт, день начинается без убытка."""
        assert _maker(tmp_path, cash=10.0).day_loss() == 0.0

    def test_a_loss_within_the_day_is_counted(self, tmp_path):
        maker = _maker(tmp_path, cash=40.0)
        maker.day_loss()                       # ставим точку отсчёта
        maker.state['cash'] = 37.0
        assert maker.day_loss() == pytest.approx(3.0)

    def test_a_profit_is_not_a_loss(self, tmp_path):
        maker = _maker(tmp_path, cash=40.0)
        maker.day_loss()
        maker.state['cash'] = 44.0
        assert maker.day_loss() == 0.0

    def test_yesterdays_hole_does_not_stop_today(self, tmp_path):
        """
        Главное свойство: вчерашние потери сегодня не считаются. Прежняя мера
        глушила торговлю навсегда, стоило счёту просесть на 5%.
        """
        maker = _maker(tmp_path, cash=30.0)     # счёт уже в глубоком минусе
        maker.state['day_anchor'] = {'day': '2000-01-01', 'equity': 40.0}
        assert maker.day_loss() == 0.0

    def test_the_anchor_moves_to_the_new_day(self, tmp_path):
        maker = _maker(tmp_path, cash=30.0)
        maker.state['day_anchor'] = {'day': '2000-01-01', 'equity': 40.0}
        maker.day_loss()
        assert maker.state['day_anchor']['equity'] == pytest.approx(30.0)
        assert maker.state['day_anchor']['day'] != '2000-01-01'

    def test_the_anchor_survives_a_restart(self, tmp_path):
        """Иначе перезапуск обнулял бы убыток и снимал предохранитель."""
        path = os.path.join(str(tmp_path), 's.json')
        first = engine.PaperMaker(bankroll=40.0, state_path=path)
        first.state['cash'] = 40.0
        first.day_loss()
        first.save()
        again = engine.PaperMaker(bankroll=40.0, state_path=path)
        again.state['cash'] = 36.0
        assert again.day_loss() == pytest.approx(4.0)


class TestABookkeepingFixIsNotALoss:
    """
    Правка учёта двигала «убыток», потому что он мерился от НАЧАЛЬНОГО счёта.
    Точка отсчёта на начало суток от этого защищает: она ставится по тому же
    исправленному капиталу.
    """

    def test_a_correction_before_the_day_starts_costs_nothing(self, tmp_path):
        maker = _maker(tmp_path, cash=32.0)     # учёт поправлен вниз
        assert maker.day_loss() == 0.0
