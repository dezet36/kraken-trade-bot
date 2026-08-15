"""
Круг не закрывается в убыток — кроме сознательного выхода по риску.

САМАЯ ДОРОГАЯ ОШИБКА, НАЙДЕННАЯ НА ЖИВЫХ КРУГАХ. Из первых четырёх закрытых
кругов три оказались убыточными, и каждый — продажей ДЕШЕВЛЕ покупки:

    купили 0.554 → продали 0.527    -$0.135
    купили 0.235 → продали 0.208    -$0.135
    купили 0.203 → продали 0.193    -$0.050
    купили 0.208 → продали 0.209    +$0.005

Причина в том, что цены считались от ТЕКУЩЕГО рынка и ничего не знали о том, по
чём мы вошли. Рынок ушёл вниз — наш аск поехал за ним и оказался ниже
собственной покупки. С виду всё правильно: обе заявки внутри спреда, обе
исполняются, — а круг в минусе. Так стратегия спреда превращается в «купить
дорого, продать дёшево».

Мейкер зарабатывает разницу цен, а не угадывание направления. Рынок ушёл —
значит круг сегодня не закроется, и это нормально: ждём, а наклон против запаса
тем временем делает покупку невыгодной, чтобы не набирать ещё.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import params, strategy  # noqa: E402

MARKET = {'tick': 0.001, 'order_min': 5, 'size': 5, 'step_ticks': 1}


def _quote(bid, ask, position=5, cost=0.0, **extra):
    top = {'bid': bid, 'ask': ask, 'mid': (bid + ask) / 2,
           'bid_size': 100, 'ask_size': 100}
    return strategy.desired_quote(top, dict(MARKET, **extra),
                                  position=position, avg_cost=cost)


class TestTheThreeLosingCirclesCannotHappenAgain:

    def test_case_one(self):
        """Купили 0.554, рынок ушёл на 0.52/0.53 — продали 0.527."""
        quote = _quote(0.520, 0.530, cost=0.554)
        assert quote['ask'] > 0.554, 'аск обязан быть выше нашей покупки'

    def test_case_two(self):
        """Купили 0.235, продали 0.208."""
        quote = _quote(0.200, 0.210, cost=0.235)
        assert quote['ask'] > 0.235

    def test_case_three(self):
        """Купили 0.203, продали 0.193."""
        quote = _quote(0.190, 0.196, cost=0.203)
        assert quote['ask'] > 0.203

    def test_a_profitable_close_is_untouched(self):
        """Рынок выше нашей покупки — правило не должно мешать."""
        quote = _quote(0.260, 0.280, cost=0.203)
        assert quote['ask'] < 0.280, 'котируем внутри спреда, как и раньше'
        assert quote['ask'] > 0.203


class TestShortSideIsSymmetric:

    def test_a_short_does_not_buy_back_higher(self):
        """Держим короткую по 0.20 — покупать обратно по 0.24 нельзя."""
        quote = _quote(0.235, 0.245, position=-5, cost=0.20)
        assert quote['bid'] < 0.20

    def test_a_short_closes_lower_normally(self):
        quote = _quote(0.150, 0.160, position=-5, cost=0.20)
        assert quote['bid'] < 0.20


class TestRiskExitOverridesTheRule:
    """
    Просроченная позиция и позиция за пределом убытка закрываются по любой
    цене: там мы уже признали, что ошиблись, и платим за выход сознательно.
    """

    def test_stale_sells_at_the_touch_even_below_cost(self):
        quote = _quote(0.520, 0.530, cost=0.554, stale=True)
        assert quote['ask'] == pytest.approx(0.530)
        assert quote['only'] == 'ask'

    def test_stale_short_buys_back_even_above_cost(self):
        quote = _quote(0.235, 0.245, position=-5, cost=0.20, stale=True)
        assert quote['bid'] == pytest.approx(0.235)
        assert quote['only'] == 'bid'


class TestNoPositionMeansNoFloor:

    def test_an_empty_book_quotes_the_market(self):
        """Без позиции ограничивать нечего — котируем как обычно."""
        quote = _quote(0.200, 0.240, position=0, cost=0.0)
        assert quote['bid'] > 0.200 and quote['ask'] < 0.240

    def test_the_cost_is_passed_from_the_engine(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert "avg_cost=slot.get('avg_cost') or 0.0" in text, \
            'без цены входа правило не работает вовсе'
