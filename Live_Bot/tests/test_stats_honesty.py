"""
Число в панели показывается вместе со своей погрешностью.

ОТКУДА ЭТО. Разбор 364 сделок с сервера 29 августа 2026. Панель показывала
«винрейт 37.7%» и «+0.012 R» голыми числами, и читались они как факты. На
деле:

    FIBO    n=193   винрейт 39.4% ± 6.9 п.п.   ожидание +0.012 ± 0.144 R
    SMC     n= 16   винрейт 18.8% ±19.1 п.п.   ожидание +0.043 ± 1.146 R
    LEVELS  n= 16   винрейт 31.2% ±22.7 п.п.   ожидание +0.009 ± 0.794 R
    RSIBB   n=  6   винрейт 33.3% ±37.7 п.п.   ожидание -0.446 ± 0.820 R

Ни одно из четырёх ожиданий не отличимо от нуля — интервал накрывает его у
всех. По голым числам принимались решения, которых они не выдерживают: «SMC
теряет 0.25R за сделку» оказалось следствием двойного учёта, а после чистки
дало +0.043 ± 1.146, то есть не значило ничего.

ВТОРОЕ. Издержки показывались только в долларах: «$46.43 комиссии и фандинг».
Доллары не с чем сравнить — много это или мало, зависит от объёма. В долях
риска они встают рядом с ожиданием, и сразу видно, кто кого съел: у FIBO
0.026 R за сделку при ожидании +0.012 R.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PANEL = os.path.join(ROOT, 'control', 'panel', 'js')
MODEL = open(os.path.join(PANEL, 'model.js'), encoding='utf-8').read()
TRADES = open(os.path.join(PANEL, 'pages', 'trades.js'), encoding='utf-8').read()
STRATEGIES = open(os.path.join(PANEL, 'pages', 'strategies.js'), encoding='utf-8').read()
SERVER = open(os.path.join(ROOT, 'control', 'dashboard.py'), encoding='utf-8').read()


def _fn(src, name):
    """Тело функции JS по имени (до парной закрывающей скобки)."""
    spot = src.index(f'function {name}(')
    depth = 0
    for i in range(src.index('{', spot), len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[spot:i + 1]
    raise AssertionError(name)


class TestTheMetricsCarryTheirError:
    """С 09.10.2026 считает панель (control/panel/js/model.js, honestStats)."""

    BODY = _fn(MODEL, 'honestStats')

    def test_the_winrate_interval_is_computed(self):
        assert 'wrCI' in self.BODY
        assert '1.96' in self.BODY and 'Math.sqrt' in self.BODY

    def test_the_expectancy_interval_is_computed(self):
        assert 'expCI' in self.BODY

    def test_one_trade_gets_no_interval(self):
        """
        Стандартная ошибка на выборке из одной сделки не определена: делить
        пришлось бы на n-1. Показывать «± NaN» хуже, чем не показывать.
        """
        assert 'n > 1' in self.BODY

    def test_the_interval_needs_the_spread_not_just_the_count(self):
        """
        Погрешность ожидания зависит от разброса результатов, а не только от
        числа сделок: десять одинаковых +1R и десять качелей от -3 до +5
        дают разную уверенность при одном n.
        """
        assert '** 2' in self.BODY or 'Math.pow' in self.BODY

    def test_trades_with_a_data_hole_are_left_out(self):
        assert '!t.suspect' in self.BODY and 'skipped' in self.BODY


class TestCostsAreComparable:

    BODY = _fn(MODEL, 'honestStats')

    def test_costs_are_expressed_in_risk(self):
        assert 'costR' in self.BODY

    def test_the_gross_expectancy_is_available(self):
        """
        Пара «с издержками / без» отвечает на вопрос, который иначе не
        задаётся: стратегия не работает или её съедают комиссии.
        """
        assert 'gross' in self.BODY

    def test_costs_include_funding_not_just_fees(self):
        """Фандинг бьёт по тем, кто держит дольше, и молчать о нём нельзя."""
        assert 'funding' in self.BODY

    def test_a_trade_without_risk_does_not_divide_by_zero(self):
        assert 't.risk ?' in self.BODY


class TestTheCardsShowIt:

    BODY = _fn(TRADES, 'honestKpis')

    def test_the_winrate_card_shows_its_error(self):
        spot = self.BODY.index("kpi('Побед'")
        assert 'wrCI' in self.BODY[spot:spot + 200]

    def test_expectancy_has_its_own_cell(self):
        assert "kpi('На сделку'" in self.BODY

    def test_an_interval_covering_zero_says_so(self):
        spot = self.BODY.index("kpi('На сделку'")
        block = self.BODY[spot:spot + 400]
        assert 'не отличить от 0' in block and 'expCI' in block

    def test_such_a_number_loses_its_colour(self):
        """
        Зелёный плюс читается как прибыль. Когда интервал накрывает ноль,
        прибыли не доказано, и красить нечего.
        """
        spot = self.BODY.index("kpi('На сделку'")
        assert "st.noise ? '' : tone(" in self.BODY[spot:spot + 400]

    def test_the_cost_card_speaks_in_risk(self):
        spot = self.BODY.index("kpi('Издержки'")
        block = self.BODY[spot:spot + 300]
        assert 'costR' in block and 'R/сд' in block
        assert 'gross' in block, 'карточка не говорит, что было бы без них'

    def test_the_strategy_page_shows_it_too(self):
        assert 'honestStats(closedTrades(data, code' in STRATEGIES
        assert 'не отличить от 0' in STRATEGIES and "st.noise ? '' : tone(" in STRATEGIES


class TestTheDisconnectedDirectionIsShouted:
    """
    «Не подключено» жило на странице «Сводка» и вместе с ней исчезло бы. Без
    ключей бот не торгует вовсе, и «почему нет сделок» иначе выясняется
    перебором разделов.
    """

    def test_the_attention_strip_knows_about_it(self):
        spot = SERVER.index('def _attention(')
        block = SERVER[spot:spot + 2000]
        assert 'connected' in block
        assert 'не подключено' in block

    def test_it_is_marked_as_blocking(self):
        """Не предупреждение, а препятствие: сделок не будет вообще."""
        spot = SERVER.index('def _attention(')
        block = SERVER[spot:spot + 2000]
        marker = block.index('не подключено')
        assert "'bad'" in block[max(0, marker - 300):marker]

    def test_it_points_where_to_go(self):
        spot = SERVER.index('def _attention(')
        block = SERVER[spot:spot + 2000]
        assert 'Подключения' in block
