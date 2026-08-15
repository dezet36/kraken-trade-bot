"""
Заявка без края — не маркет-мейкинг, а торговля по справедливой цене.

САМАЯ ДОРОГАЯ НАХОДКА ЗА ВЕСЬ РАЗБОР. Главная мерка мейкера — сколько мы берём
относительно СЕРЕДИНЫ в момент исполнения. Замер по 21 живому исполнению:

    медиана края: +0.00000
    взяли спред 10 раз, отдали или в ноль — 11

    bid 0.4350 при середине 0.4155  →  купили ВЫШЕ середины
    ask 0.1930 при середине 0.2035  →  продали НИЖЕ середины
    bid 0.7880 при середине 0.7790  →  купили ВЫШЕ середины

Нулевой край означает нулевое ожидание ДО всяких издержек. Ни число сделок, ни
охват рынков этого не лечат: умножать ноль бессмысленно.

ПРИЧИНА — допуск в два тика, поставленный ради места в очереди. Рынок сдвигался
на тик, заявка оставалась, и её подбирали ровно тогда, когда она переставала
быть выгодной. Место в очереди дорого, но не дороже смысла всей затеи.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import engine, params  # noqa: E402


def _maker(tmp_path):
    maker = engine.PaperMaker(bankroll=100, state_path=str(tmp_path / 's.json'))
    maker.state = maker._blank()
    return maker


def _book(bid, ask):
    return {'bids': [(bid, 500.0)], 'asks': [(ask, 500.0)]}


def _top(bid, ask):
    return {'bid': bid, 'ask': ask, 'mid': (bid + ask) / 2,
            'bid_size': 500.0, 'ask_size': 500.0}


class TestAQuoteWithoutEdgeIsPulled:

    def test_a_bid_that_lost_its_edge_is_replaced(self, tmp_path):
        """
        Ровно наблюдавшийся случай: наш бид 0.4350, а середина уехала на 0.4155.
        Держать такую заявку — значит платить за право купить дорого.
        """
        maker = _maker(tmp_path)
        quote = {'bid': 0.435, 'ask': 0.455, 'size': 5}
        maker.place('T', quote, _top(0.430, 0.440), _book(0.430, 0.440),
                    market_tick=0.001)
        first = maker.state['books']['T']['orders']['bid']['ts']

        # Рынок ушёл вниз меньше чем на два тика — прежний допуск заявку бы
        # сохранил, а края у неё больше нет.
        moved = {'bid': 0.434, 'ask': 0.454, 'size': 5}
        maker.place('T', moved, _top(0.410, 0.421), _book(0.410, 0.421),
                    market_tick=0.001)
        assert maker.state['books']['T']['orders']['bid']['price'] == 0.434, \
            'заявка переставлена, а не оставлена без края'
        assert maker.state['books']['T']['orders']['bid']['ts'] >= first

    def test_an_ask_that_lost_its_edge_is_replaced(self, tmp_path):
        maker = _maker(tmp_path)
        quote = {'bid': 0.180, 'ask': 0.193, 'size': 5}
        maker.place('T', quote, _top(0.180, 0.200), _book(0.180, 0.200),
                    market_tick=0.001)
        moved = {'bid': 0.181, 'ask': 0.1935, 'size': 5}
        maker.place('T', moved, _top(0.200, 0.207), _book(0.200, 0.207),
                    market_tick=0.001)
        assert maker.state['books']['T']['orders']['ask']['price'] == 0.1935


class TestAGoodQuoteKeepsItsPlaceInLine:
    """
    Перестановка стоит места в очереди, и просто так её делать нельзя: замер
    показал 4.3 перестановки на котировку, у отдельных по шестнадцать.
    """

    def test_a_quote_with_edge_survives_a_small_move(self, tmp_path):
        maker = _maker(tmp_path)
        quote = {'bid': 0.400, 'ask': 0.440, 'size': 5}
        maker.place('T', quote, _top(0.400, 0.440), _book(0.400, 0.440),
                    market_tick=0.001)
        stamp = maker.state['books']['T']['orders']['bid']['ts']

        # Сдвиг на тик, край при этом остаётся заметным.
        nudged = {'bid': 0.401, 'ask': 0.441, 'size': 5}
        maker.place('T', nudged, _top(0.401, 0.441), _book(0.401, 0.441),
                    market_tick=0.001)
        assert maker.state['books']['T']['orders']['bid']['price'] == 0.400
        assert maker.state['books']['T']['orders']['bid']['ts'] == stamp

    def test_the_threshold_is_half_a_tick(self, tmp_path):
        """
        Порог мягкий: край должен быть хотя бы в полтика, иначе держать заявку
        незачем. Жёстче — и мы вернулись бы к перестановкам на каждом дрожании.
        """
        maker = _maker(tmp_path)
        quote = {'bid': 0.500, 'ask': 0.520, 'size': 5}
        maker.place('T', quote, _top(0.500, 0.520), _book(0.500, 0.520),
                    market_tick=0.01)
        # Середина 0.5075: край нашего бида 0.0075, больше полутика (0.005).
        same = {'bid': 0.501, 'ask': 0.521, 'size': 5}
        maker.place('T', same, _top(0.501, 0.514), _book(0.501, 0.514),
                    market_tick=0.01)
        assert maker.state['books']['T']['orders']['bid']['price'] == 0.500


class TestTheRuleIsWrittenDown:

    def test_the_measurement_is_recorded_where_it_acts(self):
        text = open(os.path.join(ROOT, 'polymarket', 'engine.py'),
                    encoding='utf-8').read()
        spot = text.index('room = params.MM_REQUOTE_TICKS')
        block = text[spot:spot + 2000]
        assert 'медиана края' in block
        assert 'ВЫШЕ середины' in block
