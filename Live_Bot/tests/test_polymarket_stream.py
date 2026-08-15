"""
Подписка на стакан: единственный способ снять заявку раньше, чем её подберут.

ЗАЧЕМ. Замер по живым исполнениям: у СТОЯЩИХ котировок край +0.0095, а к
моменту исполнения от него остаётся +0.0005 — двадцатая часть. Между двумя
опросами рынок успевает пройти сквозь нашу цену, и подбирают нас ровно тогда,
когда заявка перестала быть выгодной.

Опросом это не лечится: такт уже сокращён с тридцати секунд до десяти, а
дальше упирается в предел запросов к бирже.

ПОТОК НЕ ЗАМЕНЯЕТ ОПРОС, А ОПЕРЕЖАЕТ ЕГО. Соединение рвётся, подписка отстаёт
от списка рынков, по тихому рынку сообщений может не быть часами. Поэтому у
каждой записи есть возраст, устаревшая не используется, а опрос добирает всё,
чего в потоке нет.
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import stream  # noqa: E402


class TestBookKeeping:

    def setup_method(self):
        with stream._lock:
            stream._books.clear()

    def test_a_snapshot_replaces_the_book(self):
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '100'}],
                            'asks': [{'price': '0.24', 'size': '50'}]})
        top = stream.top('T')
        assert top['bid'] == pytest.approx(0.20)
        assert top['ask'] == pytest.approx(0.24)
        assert top['mid'] == pytest.approx(0.22)
        assert top['source'] == 'поток'

    def test_a_change_brings_the_top_ready(self):
        """
        Верхушка приходит от биржи ГОТОВОЙ, и собирать её из уровней не нужно.

        Прежняя версия вела полную книгу: находила уровень по цене, заменяла
        размер, убирала при нуле. Но дельта приходит по ОДНОМУ уровню, часто
        глубокому: в захваченном сообщении цена изменения 0.162 при лучшей цене
        0.2. Собранная так книга вырождалась в 0.001/0.999, и котировки по ней
        уходили на биржу покупками по тысячной доле при рынке 0.926.
        """
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '100'}],
                            'asks': [{'price': '0.24', 'size': '50'}]})
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.162', 'size': '9431.99',
             'side': 'BUY', 'best_bid': '0.21', 'best_ask': '0.23'}]})
        top = stream.top('T')
        assert top['bid'] == pytest.approx(0.21)
        assert top['ask'] == pytest.approx(0.23)

    def test_a_deep_change_does_not_become_the_top(self):
        """Цена изменения 0.162 при лучшей 0.2 — это глубокий уровень."""
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '100'}],
                            'asks': [{'price': '0.24', 'size': '50'}]})
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.001', 'size': '5',
             'side': 'BUY', 'best_bid': '0.20', 'best_ask': '0.24'}]})
        assert stream.top('T')['bid'] == pytest.approx(0.20)

    def test_a_nonsense_top_is_ignored(self):
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '100'}],
                            'asks': [{'price': '0.24', 'size': '50'}]})
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.5', 'size': '5', 'side': 'BUY',
             'best_bid': '0.9', 'best_ask': '0.1'}]})
        assert stream.top('T')['bid'] == pytest.approx(0.20), 'перекос отвергнут'

    def test_a_crossed_book_is_refused(self):
        """Перекрещенной книге верить нельзя — лучше вернуться к опросу."""
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.30', 'size': '10'}],
                            'asks': [{'price': '0.20', 'size': '10'}]})
        assert stream.top('T') is None

    def test_stale_data_is_not_used(self):
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '10'}],
                            'asks': [{'price': '0.24', 'size': '10'}]})
        with stream._lock:
            stream._books['T']['at'] = time.time() - stream.FRESH_SECONDS - 1
        assert stream.top('T') is None
        assert stream.book('T') is None

    def test_an_unknown_token_is_not_an_error(self):
        assert stream.top('НЕТ ТАКОГО') is None
        assert stream.book('НЕТ ТАКОГО') is None


class TestSubscriptionFollowsTheMarkets:

    def test_watching_a_new_set_asks_for_resubscribe(self):
        stream._resubscribe.clear()
        stream.watch(['A', 'B'])
        assert stream._resubscribe.is_set()

    def test_the_same_set_does_not(self):
        stream.watch(['A', 'B'])
        stream._resubscribe.clear()
        stream.watch(['B', 'A'])
        assert not stream._resubscribe.is_set(), 'лишний разрыв стоит секунд'

    def test_status_reports_what_matters(self):
        got = stream.status()
        for key in ('connected', 'messages', 'books', 'fresh', 'last_error'):
            assert key in got


class TestPollingStaysAsTheFallback:

    def test_step_asks_rest_only_for_what_the_stream_lacks(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('def step(')
        block = text[spot:spot + 3000]
        assert 'stream.book(token)' in block
        assert 'ask_rest' in block
        assert 'book_mod.fetch_many(ask_rest)' in block

    def test_the_service_survives_a_dead_stream(self):
        """Подписка не поднялась — работаем опросом, а не встаём."""
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        spot = text.index('stream.start(')
        assert 'работаю опросом' in text[spot:spot + 500]

    def test_the_system_resolver_is_forced(self):
        """
        При установленном aiodns aiohttp ходит к DNS сам, мимо системы, и в
        сети с корпоративным DNS это кончается «Could not contact DNS servers».
        Поймано здесь: REST-адреса работали, адрес потока не разрешался ни разу.
        """
        text = open(os.path.join(ROOT, 'polymarket', 'stream.py'),
                    encoding='utf-8').read()
        assert 'ThreadedResolver()' in text


class TestDeltasWithoutASnapshotAreNotABook:
    """
    Изменения уровней приходят и ДО первого снимка, и по ним собирается огрызок
    из двух-трёх цен. Лучшая цена по такому огрызку выдумана.

    Наблюдалось в работе: «край 0.119» там, где спред рынка полтора цента, и
    одиннадцать котировок из двадцати пяти оказались без края вовсе.
    """

    def setup_method(self):
        with stream._lock:
            stream._books.clear()

    def test_a_delta_without_a_top_gives_nothing(self):
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.21', 'size': '30', 'side': 'BUY'}]})
        assert stream.top('T') is None, 'без готовой верхушки цену не выдумываем'

    def test_a_snapshot_makes_it_usable(self):
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.21', 'size': '30', 'side': 'BUY'}]})
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '100'}],
                            'asks': [{'price': '0.24', 'size': '50'}]})
        assert stream.top('T') is not None

    def test_deltas_after_a_snapshot_still_work(self):
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '100'}],
                            'asks': [{'price': '0.24', 'size': '50'}]})
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.22', 'size': '10', 'side': 'BUY',
             'best_bid': '0.22', 'best_ask': '0.23'}]})
        assert stream.top('T')['bid'] == pytest.approx(0.22)

    def test_unsynced_books_are_not_counted_as_fresh(self):
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.21', 'size': '30', 'side': 'BUY'}]})
        assert stream.status()['fresh'] == 0


class TestOrdersFromThePastAreNotInherited:
    """
    При запуске на бирже могут стоять наши же заявки от прошлого прогона —
    выставленные другой версией кода, по другим ценам, из книги, которой мы
    больше не верим.

    Наблюдалось прямо в работе, после первого включения потока с неполными
    книгами:

        аск по 0.998 при середине 0.079
        бид по 0.010 при середине 0.545

    Такие не исполнятся, но занимают деньги, засоряют стакан и портят замер
    края: медиана по всем котировкам показывала 0.119 при спреде рынка в
    полтора цента.
    """

    def test_the_service_cancels_them_at_start(self):
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        body = text[text.index('def _loop('):text.index('def start(')]
        spot = body.index('executor.cancel_all()')
        assert spot < body.index('while True:'), 'снимать надо ДО первого такта'
        assert 'заявки прошлого прогона' in body

    def test_our_own_record_is_cleared_too(self):
        """
        Снять на бирже мало: у нас остаётся память о заявках, которых больше
        нет. Иначе сверка увидит призраков и решит, что мы котируем.
        """
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        spot = text.index('заявки прошлого прогона')
        assert 'forget_orders' in text[spot:spot + 400]

    def test_paper_mode_has_nothing_to_cancel(self):
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        spot = text.index('заявки прошлого прогона')
        assert 'if live:' in text[max(0, spot - 700):spot]


class TestTheStreamIsOffUntilItIsProven:
    """
    ЧЕСТНЫЙ ИТОГ ПРОВЕРКИ. Сам поток работает: снимок приходит верный, до цифры
    совпадает с опросом — 0.026/0.036 при 14+34 уровнях, и параметр `level` тут
    ни при чём.

    А вот после применения присылаемых ИЗМЕНЕНИЙ книга вырождается в
    0.001/0.999, то есть в одни крайние уровни. Котировки по такой книге уходили
    на биржу покупками по 0.001 при рынке 0.926: не исполнятся никогда, но
    занимают деньги и засоряют стакан. Семнадцать таких заявок за один запуск.

    Опрос от этого защищён по построению — он всегда приносит книгу целиком.
    Поэтому поток остаётся под настройкой до конца разбора.
    """

    def test_the_stream_is_on_again(self):
        """Причина вырождения книги найдена и устранена — можно включать."""
        from polymarket import params

        assert params.MM_STREAM is True

    def test_the_cycle_asks_the_stream_only_when_allowed(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        spot = text.index('def step(')
        block = text[spot:spot + 3000]
        assert 'stream.book(token) if params.MM_STREAM else None' in block
        assert 'if params.MM_STREAM:' in block

    def test_the_service_says_which_way_it_works(self):
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        assert 'работаю опросом' in text

    def test_the_reason_is_written_down_where_the_switch_is(self):
        text = open(os.path.join(ROOT, 'polymarket', 'params.py'),
                    encoding='utf-8').read()
        spot = text.index('MM_STREAM = _b(')
        block = text[max(0, spot - 1400):spot]
        assert '0.001/0.999' in block, 'что было сломано'
        assert 'best_bid' in block, 'и чем оказалось починено'


class TestTheSnapshotIsSortedBestFirst:
    """
    ОШИБКА, СТАВИВШАЯ ЗАЯВКИ ПО ЦЕНЕ В ОДИН ЦЕНТ НА РЫНКЕ В СОРОК ТРИ.

    `book.top` берёт просто ПЕРВЫЙ уровень — так устроен опрос, который
    сортирует сам. Площадка присылает биды по ВОЗРАСТАНИЮ, а снимок из потока
    не сортировался, и лучшим бидом оказывался худший:

        «Theo FDV»,   16 уровней: первый бид 0.01, настоящий 0.39
        «CDU Berlin», 56 уровней: первый бид 0.01, настоящий 0.66

    Обе заявки ушли на биржу по 0.01 и стояли за очередью в двадцать пять тысяч
    контрактов. Прошлая правка потока чинила ДЕЛЬТЫ и про снимок молчала.
    """

    def _snapshot(self):
        stream._books.clear()
        stream._apply_book({
            'asset_id': 'T',
            # Порядок биржи: биды по возрастанию, аски по убыванию.
            'bids': [{'price': '0.01', 'size': '10052.35'},
                     {'price': '0.02', 'size': '500'},
                     {'price': '0.39', 'size': '120'}],
            'asks': [{'price': '0.99', 'size': '35507.08'},
                     {'price': '0.97', 'size': '23000'},
                     {'price': '0.47', 'size': '90'}],
        })
        return stream.book('T')

    def test_the_best_bid_comes_first(self):
        assert self._snapshot()['bids'][0][0] == 0.39

    def test_the_best_ask_comes_first(self):
        assert self._snapshot()['asks'][0][0] == 0.47

    def test_book_top_reads_it_the_same_way(self):
        """Опрос и поток обязаны отдавать книгу в одном виде."""
        from polymarket import book as book_mod
        top = book_mod.top(self._snapshot())
        assert top['bid'] == 0.39
        assert top['ask'] == 0.47
        assert top['mid'] == 0.43

    def test_the_stream_top_agrees(self):
        self._snapshot()
        assert stream.top('T')['bid'] == 0.39
