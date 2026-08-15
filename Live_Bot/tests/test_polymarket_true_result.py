"""
Итог считается по бирже, а запас — по биду.

ЖАЛОБА: «показывает, что полимаркет принёс 290 долларов прибыли, а на самом
деле у меня на аккаунте минус 2 доллара».

Первая половина расхождения была грубой и уже исправлена: панель складывала
БУМАЖНУЮ модель. Вторая оказалась тоньше и осталась: модель оценивает запас по
СЕРЕДИНЕ рынка, а получить за него можно только по БИДУ.

    панель показывала  -$0.04
    на счёте было      -$2.89

Вся разница сидела ровно в выборе цены — на двенадцати позициях. Середина это
цена, по которой никто не обязан у нас покупать; бид — та, по которой купят
сегодня. К тому же оценка по середине льстит систематически: нас исполняют,
когда цена идёт против нас, поэтому запас почти всегда стоит ближе к биду.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

import polymarket  # noqa: E402


class TestHoldingsAreValuedAtTheBid:

    def _books(self, monkeypatch, bid, ask):
        from polymarket import book as book_mod

        monkeypatch.setattr(book_mod, 'fetch_many', lambda tokens: {
            str(t): {'bids': [(bid, 500.0)], 'asks': [(ask, 500.0)]}
            for t in tokens})

    def test_value_uses_the_bid_not_the_middle(self, monkeypatch):
        self._books(monkeypatch, bid=0.40, ask=0.50)
        got = polymarket._holdings_value(
            [{'token': 'T', 'side': 'bid', 'price': 0.45, 'size': 10}])
        assert got['positions_value'] == pytest.approx(4.0), 'по биду, а не по 0.45'
        assert got['positions_paid'] == pytest.approx(4.5)
        assert got['positions_count'] == 1

    def test_a_closed_position_leaves_nothing(self, monkeypatch):
        self._books(monkeypatch, bid=0.40, ask=0.50)
        got = polymarket._holdings_value([
            {'token': 'T', 'side': 'bid', 'price': 0.45, 'size': 10},
            {'token': 'T', 'side': 'ask', 'price': 0.47, 'size': 10},
        ])
        assert got['positions_count'] == 0
        assert got['positions_value'] == 0.0
        assert got['positions_paid'] == pytest.approx(-0.2), 'закрыли в плюс'

    def test_no_trades_means_no_holdings(self):
        got = polymarket._holdings_value([])
        assert got == {'positions_value': 0.0, 'positions_paid': 0.0,
                       'positions_count': 0}

    def test_unreachable_book_says_unknown_not_zero(self, monkeypatch):
        """
        Ноль и «не удалось спросить» — разные вещи. Ноль означал бы, что запас
        обесценился, а это совсем другой разговор.
        """
        from polymarket import book as book_mod

        def boom(tokens):
            raise OSError('сеть закрыта')

        monkeypatch.setattr(book_mod, 'fetch_many', boom)
        got = polymarket._holdings_value(
            [{'token': 'T', 'side': 'bid', 'price': 0.45, 'size': 10}])
        assert got['positions_value'] is None
        assert got['positions_count'] == 1


class TestPanelShowsTheExchangeResult:

    def _html(self):
        return open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8').read()

    def test_the_result_is_built_from_balance_and_holdings(self):
        html = self._html()
        assert 'ex.positions_value' in html
        assert 'ex.balance + held' in html

    def test_the_bottom_line_sits_in_the_exchange_block(self):
        html = self._html()
        spot = html.index('Что видно на бирже')
        block = html[max(0, spot - 2500):spot]
        assert 'Итог к вложенному' in block
        assert 'Токены на руках' in block

    def test_the_choice_of_price_is_explained(self):
        text = open(os.path.join(ROOT, 'polymarket', '__init__.py'),
                    encoding='utf-8').read()
        spot = text.index('def _holdings_value')
        assert 'ПО БИДУ, А НЕ ПО СЕРЕДИНЕ' in text[spot:spot + 900]
