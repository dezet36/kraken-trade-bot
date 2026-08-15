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

import pytest  # noqa: E402

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


class TestEdgeIsMeasuredEveryCycle:
    """
    Край виден у каждой стоящей заявки прямо сейчас — просто сравнением с
    серединой. Мерить его по ИСПОЛНЕНИЯМ слишком медленно: их единицы в сутки,
    и чтобы понять, работает ли стратегия, пришлось бы ждать днями.
    """

    def test_step_writes_the_edge_journal(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert 'EDGES = os.path.join' in text
        spot = text.index('def step(')
        block = text[spot:]
        assert 'quoted_edges' in block
        assert "store._append(EDGES" in block

    def test_the_summary_names_quotes_without_edge(self):
        import polymarket

        got = polymarket._edge_summary([
            {'median': 0.01, 'quotes': 10, 'without_edge': 0},
            {'median': 0.008, 'quotes': 12, 'without_edge': 2},
        ])
        assert got['count'] == 2
        assert got['now'] == 0.008
        assert got['without_edge'] == 2
        assert got['quotes'] == 12

    def test_no_measurements_is_not_a_zero_edge(self):
        import polymarket

        assert polymarket._edge_summary([]) == {'count': 0}

    def test_the_panel_shows_it_before_the_slower_checks(self):
        html = open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()
        assert 'Берём ли мы спред' in html
        assert (html.index('Берём ли мы спред')
                < html.index('Обещание модели против дела')), \
            'главная мерка стоит выше медленных'


class TestEdgeIsMeasuredWhenWePlace:
    """
    КРАЙ НЕЛЬЗЯ СЧИТАТЬ ОТ СЕРЕДИНЫ В МОМЕНТ ИСПОЛНЕНИЯ, и это ошибка самого
    замера, из-за которой вывод «медиана края ноль» оказался отчасти артефактом.

    Об исполнении мы узнаём в СЛЕДУЮЩЕМ такте и берём середину оттуда. Но
    сделка сама двигает книгу: покупка у нас толкает середину вниз, и наш же
    край выглядит меньше, чем был. Замерять надо от середины в момент, когда мы
    ВСТАЛИ, — это и есть край, на который мы рассчитывали.

    Снос цены ПОСЛЕ исполнения остаётся отдельным числом: это другой вопрос —
    не выбирают ли нас систематически.
    """

    def test_the_order_remembers_the_mid_it_was_placed_at(self, tmp_path):
        maker = _maker(tmp_path)
        quote = {'bid': 0.400, 'ask': 0.440, 'size': 5}
        maker.place('T', quote, _top(0.400, 0.440), _book(0.400, 0.440),
                    market_tick=0.001)
        order = maker.state['books']['T']['orders']['bid']
        assert order['mid_at_place'] == pytest.approx(0.420)

    def test_the_edge_is_computed_from_that_mid(self, tmp_path):
        maker = _maker(tmp_path)
        maker.state['drift_pending'] = [{
            'token': 'T', 'side': 'bid', 'price': 0.400, 'size': 5,
            'mid_at_fill': 0.401, 'mid_at_place': 0.420,
            'ts': engine._now() - 10_000}]
        ripe = maker.measure_drift({'T': 0.405})
        assert ripe and ripe[0]['edge_at_place'] == pytest.approx(0.020), \
            'край считается от середины при выставлении, а не при исполнении'

    def test_a_sell_edge_has_the_right_sign(self, tmp_path):
        maker = _maker(tmp_path)
        maker.state['drift_pending'] = [{
            'token': 'T', 'side': 'ask', 'price': 0.440, 'size': 5,
            'mid_at_fill': 0.439, 'mid_at_place': 0.420,
            'ts': engine._now() - 10_000}]
        ripe = maker.measure_drift({'T': 0.435})
        assert ripe[0]['edge_at_place'] == pytest.approx(0.020)

    def test_an_old_record_without_the_mid_is_not_a_zero_edge(self, tmp_path):
        """Записи прежних прогонов не должны притворяться нулевым краем."""
        maker = _maker(tmp_path)
        maker.state['drift_pending'] = [{
            'token': 'T', 'side': 'bid', 'price': 0.400, 'size': 5,
            'mid_at_fill': 0.401, 'ts': engine._now() - 10_000}]
        ripe = maker.measure_drift({'T': 0.405})
        assert ripe[0]['edge_at_place'] is None

    def test_drift_still_measures_what_happened_after(self, tmp_path):
        maker = _maker(tmp_path)
        maker.state['drift_pending'] = [{
            'token': 'T', 'side': 'bid', 'price': 0.400, 'size': 5,
            'mid_at_fill': 0.400, 'mid_at_place': 0.420,
            'ts': engine._now() - 10_000}]
        ripe = maker.measure_drift({'T': 0.410})
        assert ripe[0]['gain_per_contract'] == pytest.approx(0.010)


class TestOnlyRealOrdersAreMeasured:
    """
    В состоянии остаются заявки, которых на бирже нет: не ушедшие, снятые при
    запуске, оставшиеся от прежних прогонов. Их цены старые, и край по ним —
    выдумка.

    Замер показывал «медиана 0.45, десять котировок без края» при двадцати
    шести учтённых и ТРИНАДЦАТИ настоящих: считались призраки.
    """

    def test_the_measurement_skips_orders_without_an_exchange_number(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('quoted_edges = []')
        block = text[spot:spot + 1600]
        assert "if live and not order.get('live_id'):" in block

    def test_paper_mode_still_measures_its_own_quotes(self):
        """
        В бумаге биржевого номера нет ни у одной заявки — и мерить всё равно
        надо, иначе бумажный прогон остался бы без главной мерки.
        """
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('quoted_edges = []')
        block = text[spot:spot + 1600]
        assert 'if live and' in block, 'условие только для живого режима'


class TestFlowIsNotABurst:
    """
    ГЛАВНАЯ ПРИЧИНА, ПО КОТОРОЙ НИЧЕГО НЕ ИСПОЛНЯЛОСЬ.

    Знаменателем скорости служил промежуток от первой сделки до последней — и
    сгусток превращался в мнимую скорость. Замерено на живых рынках, где мы
    стояли заявками:

        «Republicans win Arkansas»  7 сделок за сутки, все за 15 минут
                                    старый счёт: 27.5 сделок в час
                                    на деле:      0.3 в час, тишина 2.9ч

    Медиана по нашим шестнадцати рынкам: НОЛЬ сделок в час, а с последней
    сделки прошло 6.6 часа. При этом девять заявок из семнадцати стояли ПЕРВЫМИ
    в очереди — позиция была прекрасной, торговать было не с кем.

    ПЕРЕНОСА ЗНАМЕНАТЕЛЯ НА «ДО СЕЙЧАС» ОКАЗАЛОСЬ МАЛО. Усреднение за сутки всё
    равно выдавало вчерашнюю толчею за сегодняшний поток: следующий отбор обещал
    круг в 46 минут на рынке, молчавшем восемь часов. Вес сделки теперь падает
    как exp(−возраст/срок) — ровный рынок оценивается по-прежнему, сгусток
    двадцатичасовой давности сжимается в семьсот раз.
    """

    def _rate(self, monkeypatch, ages_hours):
        """Считает поток по сделкам, случившимся столько-то часов назад."""
        import time

        from polymarket import book as book_mod, selector

        now = time.time()
        trades = [{'ts': now - h * 3600, 'price': 0.20, 'size': 10.0,
                   'side': 'SELL', 'asset': 'T'} for h in ages_hours]
        monkeypatch.setattr(book_mod, 'tape_many', lambda conds: {'C': trades})
        book = {'bids': [(0.20, 500.0)], 'asks': [(0.24, 500.0)]}
        row = {'token_id': 'T', 'token_no': 'N', 'condition_id': 'C',
               'tick': 0.01, 'order_min': 5, 'size': 5,
               'bid_usd': 500.0, 'ask_usd': 500.0}
        selector.measure_wait([row], {'T': book})
        return row

    def test_an_old_burst_is_not_a_fast_market(self, monkeypatch):
        """
        Пять сделок по десять контрактов, все двадцать часов назад. Рынок с тех
        пор молчит — и звать нас туда нечему. Прежний счёт делил на промежуток
        МЕЖДУ сделками и давал сто контрактов в час; вес свежести оставляет от
        сгустка сотые доли.
        """
        row = self._rate(monkeypatch, [20.0, 20.05, 20.1, 20.15, 20.2])
        assert row['flow_in'] < 0.1

    def test_a_steady_market_keeps_its_rate(self, monkeypatch):
        """
        Ровный рынок оценивается ЧЕСТНО, а не занижается заодно со сгустком.

        Десять контрактов в час ровно сутки подряд обязаны и остаться десятью:
        вес и знаменатель — один и тот же интеграл, и для постоянной скорости
        они сокращаются.
        """
        row = self._rate(monkeypatch, [h + 0.5 for h in range(24)])
        assert 8 < row['flow_in'] < 12

    def test_silence_lowers_the_rate(self, monkeypatch):
        """Та же горсть сделок, но давно — скорость обязана быть ниже."""
        fresh = self._rate(monkeypatch, [0.2, 0.4, 0.6])
        stale = self._rate(monkeypatch, [18.0, 18.2, 18.4])
        assert stale['flow_in'] < fresh['flow_in'] / 50

    def test_yesterdays_rush_does_not_promise_a_fast_round_trip(self,
                                                               monkeypatch):
        """
        Замер, ради которого всё и переделано.

        Восемьсот контрактов прошли за двадцать минут — и было это восемь часов
        назад. Прежний счёт делил их на промежуток МЕЖДУ сделками и обещал круг
        за двадцать две секунды. Вес свежести оставляет от той скорости
        девятипроцентную долю, и обещание становится полчаса с лишним —
        поправка в полтораста раз.

        Больше от этой заготовки требовать нечего: рынок и правда торговал, и
        сказать «никогда» о нём было бы такой же неправдой, как «сейчас».
        """
        import time

        from polymarket import book as book_mod, selector

        now = time.time()
        trades = [{'ts': now - (8 + i * 0.02) * 3600, 'price': p, 'size': 20.0,
                   'side': s, 'asset': 'T'}
                  for i in range(20) for p, s in [(0.20, 'SELL'),
                                                  (0.24, 'BUY')]]
        monkeypatch.setattr(book_mod, 'tape_many', lambda conds: {'C': trades})
        row = {'token_id': 'T', 'token_no': 'N', 'condition_id': 'C',
               'tick': 0.01, 'order_min': 5, 'size': 5,
               'bid_usd': 500.0, 'ask_usd': 500.0}
        selector.measure_wait([row], {'T': {'bids': [(0.20, 500.0)],
                                            'asks': [(0.24, 500.0)]}})
        assert row['wait_hours'] > 0.5

    def test_the_step_is_chosen_by_income_not_by_spread(self, monkeypatch):
        """
        ОЧЕРЕДЬ ВХОДИТ В ВЫБОР ШАГА, и без неё выбиралось худшее.

        Спред три тика, поток 158 контрактов в час, впереди 2000:

            шаг 0:  берём 0.03, но стоим за очередью → круг 18.8 часа
            шаг 1:  берём 0.01 при пустой очереди    → круг 0.05 часа

        Прежняя мера — «спред на поток» — предпочитала первый, потому что
        тройной спред перевешивал. А он в четыреста раз медленнее, и рынок
        отсеивался как безнадёжный при круге в три минуты.
        """
        import time

        from polymarket import book as book_mod, selector

        now = time.time()
        trades = [{'ts': now - 600, 'price': 0.49, 'size': 500.0,
                   'side': 'SELL', 'asset': 'T'},
                  {'ts': now - 300, 'price': 0.52, 'size': 500.0,
                   'side': 'BUY', 'asset': 'T'}]
        monkeypatch.setattr(book_mod, 'tape_many', lambda conds: {'C': trades})
        row = {'token_id': 'T', 'token_no': 'N', 'condition_id': 'C',
               'tick': 0.01, 'order_min': 5, 'size': 5,
               'bid_usd': 1000.0, 'ask_usd': 1000.0}
        selector.measure_wait([row], {'T': {'bids': [(0.49, 2000.0)],
                                            'asks': [(0.52, 2000.0)]}})
        assert row['step_ticks'] == 1, 'встали бы за очередь из двух тысяч'
        assert row['wait_hours'] < 1

    def test_the_weight_decays_with_age(self):
        text = open(os.path.join(ROOT, 'polymarket', 'selector.py'),
                    encoding='utf-8').read()
        assert "t['weight'] = math.exp(-((now - t['ts']) / 3600) / decay)" in text
        assert "t['size'] * t['weight']" in text
