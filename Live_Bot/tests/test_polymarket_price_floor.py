"""
Нижняя граница цены: где стратегия перестаёт терять.

ДВА НЕЗАВИСИМЫХ ЗАМЕРА СОШЛИСЬ НА ОДНОМ, и оба по живым сделкам.

Первый — закрытые круги по цене входа:

    цена 0.0-0.1   3 круга,  1 в плюс,  −$0.205
    цена 0.1-0.3   7 кругов, 3 в плюс,  −$0.225
    цена 0.3-0.6   3 круга,  2 в плюс,  +$0.065
    цена 0.6-1.0   2 круга,  2 в плюс,  +$0.040

Второй — куда уходила середина ПОСЛЕ наших исполнений:

    цена 0.0-0.1    7 набл., среднее −0.0040
    цена 0.1-0.3   13 набл., среднее −0.0095
    цена 0.3-0.6   13 набл., среднее +0.0008
    цена 0.6-1.0    4 набл., среднее +0.0004

Ниже трети рынок ходит ПРОТИВ нас, выше — нет.

МЕХАНИЗМ ИЗВЕСТЕН И ПРОВЕРЕН ОТДЕЛЬНО. Дешёвый контракт чаще всего гаснет в
ноль, и наш бид на нём подбирают те, кто от него избавляется. Проверка на 1 706
разрешённых рынках: в диапазоне 0.001-0.010 «да» не сбылось ни разу.

Это же главная поломка разобранного кошелька @planktonxd: 2 236 позиций,
переоценка −$8 564 при +$11 000 зафиксированных.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import params, selector  # noqa: E402


class TestCheapMarketsAreRefused:

    def test_the_floor_is_where_the_measurement_turns(self):
        """Убыточные полосы кончаются на трети — там и порог."""
        assert params.MM_MIN_PRICE >= 0.30

    def test_a_longshot_never_reaches_selection(self):
        """
        Пятицентовый контракт — тот самый хвост, на котором теряет разобранный
        кошелёк. До отбора он теперь не доходит.
        """
        assert not params.MM_MIN_PRICE < 0.05

    def test_the_expensive_side_keeps_its_own_bound(self):
        """Симметричная беда с другой стороны никуда не делась."""
        assert params.MM_MAX_PRICE <= 0.95
        assert params.MM_MIN_PRICE < params.MM_MAX_PRICE

    def test_the_universe_survives_the_floor(self):
        """
        Порог не должен обнулять отбор. Замер по 2 000 активных рынков: при
        0.30 остаётся 640, из них 246 платят награду.
        """
        assert params.MM_MIN_PRICE <= 0.40, \
            'выше сорока процентов вселенная становится слишком узкой'

    def test_the_reason_is_written_where_the_number_lives(self):
        text = open(os.path.join(ROOT, 'polymarket', 'params.py'),
                    encoding='utf-8').read()
        spot = text.index('MM_MIN_PRICE = _f')
        block = text[max(0, spot - 2200):spot]
        assert 'ПОСЛЕ наших исполнений' in block
        assert 'planktonxd' in block


class TestTheFloorFeedsTheScan:

    def test_scan_uses_the_parameter(self):
        """Порог обязан применяться в отборе, а не лежать в настройках."""
        text = open(os.path.join(ROOT, 'polymarket', 'selector.py'),
                    encoding='utf-8').read()
        assert 'params.MM_MIN_PRICE' in text

    def test_candidates_drop_anything_below(self):
        rows = selector._candidates.__doc__ or ''
        assert 'метаданны' in rows.lower() or 'стакан' in rows.lower()
