"""
Вход дешёвой стороной: главное отличие от разобранного кошелька.

ОНО НЕ В МЕТОДЕ, А В ТОМ, СКОЛЬКО ДЕНЕГ СВЯЗЫВАЕТ ОДНА КОТИРОВКА.

    @planktonxd:  покупает контракт по 0.09, продаёт по 0.105
                  занято $0.09, заработано $0.015  →  16% на вложенное
    мы (было):    двусторонняя котировка в пять контрактов стоит РОВНО $5 при
                  любой цене, заработок $0.06      →  1.2% на вложенное

Причина проста: наш аск на дешёвом рынке — это покупка встречного токена по
0.94. Мы занимали девяносто четыре цента, чтобы заработать семь десятых. Он так
не делает: покупает дёшево, а продаёт ТО, ЧТО УЖЕ ДЕРЖИТ, — а продажа своего не
стоит ничего.

ЗАМЕРЕНО ПО 327 РЫНКАМ С ЖИВОЙ КНИГОЙ:

    двусторонняя котировка: $5.00 на рынок →  8 рынков на $40
    вход дешёвой стороной:  $0.62 на рынок → 64 рынка на те же $40

    цена 0.02-0.10: 136 рынков, занимаем $0.28, круг даёт 10.9% на вложенное
    цена 0.10-0.25:  58 рынков, занимаем $0.78, круг даёт  6.5%
    цена 0.25-0.50:  69 рынков, занимаем $1.68, круг даёт  3.0%

Плата — направленность между входом и выходом: обе стороны больше не стоят
одновременно. Держат её срок удержания, предел убытка и наклон против запаса —
ровно то, чего не было у разобранного кошелька с его 2 236 позициями и
переоценкой -$8 564.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import params, selector, strategy  # noqa: E402

MARKET = {'tick': 0.01, 'order_min': 5, 'size': 5, 'step_ticks': 0}


def _quote(price, position=0, cost=0.0, **extra):
    top = {'bid': round(price - 0.01, 4), 'ask': round(price + 0.01, 4),
           'mid': price, 'bid_size': 100, 'ask_size': 100}
    return strategy.desired_quote(top, dict(MARKET, **extra),
                                  position=position, avg_cost=cost)


class TestWeEnterOnTheCheapSide:

    def test_a_cheap_market_is_entered_by_buying(self):
        """Контракт по 0.06 стоит шесть центов; встречный — девяносто четыре."""
        quote = _quote(0.06)
        assert quote['only'] == 'bid'

    def test_an_expensive_market_is_entered_by_the_other_side(self):
        """При цене 0.85 дешевле встречный токен: 0.15 против 0.85."""
        quote = _quote(0.85)
        assert quote['only'] == 'ask'

    def test_the_middle_still_picks_a_side(self):
        quote = _quote(0.50)
        assert quote['only'] in ('bid', 'ask')

    def test_holding_switches_to_the_free_side(self):
        """Держим — продажа своего не стоит ничего, ею и выходим."""
        assert _quote(0.06, position=5, cost=0.06)['only'] == 'ask'
        assert _quote(0.85, position=-5, cost=0.85)['only'] == 'bid'


class TestCostFallsWithOneSidedEntry:

    def test_two_sided_costs_exactly_the_size(self):
        for price in (0.02, 0.1, 0.5, 0.9):
            assert selector.quote_cost(5, price, one_sided=False) == pytest.approx(5.0)

    def test_one_sided_costs_the_cheaper_leg(self):
        assert selector.quote_cost(5, 0.06, one_sided=True) == pytest.approx(0.30)
        assert selector.quote_cost(5, 0.85, one_sided=True) == pytest.approx(0.75)

    def test_the_saving_is_biggest_where_the_edge_is_biggest(self):
        """Дешёвая полоса — и лучший процент, и самая большая экономия."""
        cheap = selector.quote_cost(5, 0.06, one_sided=True)
        middle = selector.quote_cost(5, 0.45, one_sided=True)
        assert cheap < middle / 5

    def test_the_setting_decides_by_default(self, monkeypatch):
        monkeypatch.setattr(params, 'MM_CAPITAL_LIGHT', False)
        assert selector.quote_cost(5, 0.06) == pytest.approx(5.0)
        monkeypatch.setattr(params, 'MM_CAPITAL_LIGHT', True)
        assert selector.quote_cost(5, 0.06) == pytest.approx(0.30)


class TestBudgetCountsTheRealCost:

    def test_step_charges_only_the_side_it_posts(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert "if quote.get('only') == 'bid':" in text
        assert "need = float(quote['size']) * float(quote['bid'])" in text

    def test_closing_a_position_is_free(self):
        """Продаём то, что держим: новых денег это не требует."""
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index("if quote.get('only') == 'bid':")
        block = text[spot:spot + 900]
        assert 'need = 0.0' in block
        assert 'продаём то, что держим' in block.lower()


class TestTheRiskThatComesWithIt:
    """
    Плата за экономию — направленность: обе стороны больше не стоят
    одновременно. Держать её должны те же три ограничения.
    """

    def test_the_hold_limit_still_applies(self):
        quote = _quote(0.06, position=5, cost=0.06, stale=True)
        assert quote['only'] == 'ask'

    def test_the_loss_limit_still_applies(self):
        assert params.MM_MAX_POSITION_LOSS > 0

    def test_the_skew_still_pushes_the_entry_away(self):
        """Набрали — покупать должно стать невыгодно."""
        flat = _quote(0.22, position=0)
        loaded = _quote(0.22, position=5, cost=0.22)
        assert loaded['bid'] < flat['bid']
