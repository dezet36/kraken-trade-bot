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

    def test_a_change_updates_one_level(self):
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '100'}],
                            'asks': [{'price': '0.24', 'size': '50'}]})
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.21', 'size': '30', 'side': 'BUY'}]})
        assert stream.top('T')['bid'] == pytest.approx(0.21)

    def test_a_zero_size_removes_the_level(self):
        """Нулевой размер означает, что уровень исчез, а не что он пустой."""
        stream._apply_book({'asset_id': 'T',
                            'bids': [{'price': '0.20', 'size': '100'},
                                     {'price': '0.21', 'size': '10'}],
                            'asks': [{'price': '0.24', 'size': '50'}]})
        stream._apply_change({'price_changes': [
            {'asset_id': 'T', 'price': '0.21', 'size': '0', 'side': 'BUY'}]})
        assert stream.top('T')['bid'] == pytest.approx(0.20)

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
