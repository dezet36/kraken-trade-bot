"""
ЗЕРКАЛЬНАЯ ПРОДАЖА: позиция переворачивается, деньги — нет.

Продать «ДА», которого нет, биржа не даст. Продажа уходит покупкой «НЕТ» и
обратно приходит сделкой встречного токена. Перевод к нашей стороне верен для
ПОЗИЦИИ — ставка та же. Для ДЕНЕГ он перевёрнут: продажа приносит p за контракт,
покупка встречного забирает (1−p).

Учёт считал по переведённой стороне и ошибался на p + (1−p), то есть ровно на
доллар с каждого контракта. Замерено на живом счёте:

    наша бухгалтерия: $29.03 наличными
    биржа:            $ 1.45

Весь счёт при этом тихо перетёк в запас — шестнадцать позиций на $38.77. Каждая
«продажа» делала книги богаче, а счёт беднее, и бот раз за разом строил план на
сорок долларов, получая «не хватает денег».

ВАЖНО, ЧТО ОШИБОК БЫЛО ДВЕ И ОНИ ГАСИЛИ ДРУГ ДРУГА. Минус по «ДА» оценивался по
цене «ДА» вместо (1 − цена), и в сумме итог сходился при двух неверных
слагаемых. Поправить одно значило сломать целое, поэтому здесь проверяются обе.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import engine, mm  # noqa: E402

MARKETS = [{'token_id': 'YES', 'token_no': 'NO', 'question': 'рынок'}]


def _maker(cash=100.0, tmp_path=None):
    made = engine.PaperMaker(bankroll=cash,
                             state_path=os.path.join(str(tmp_path), 's.json'))
    made.state['cash'] = cash
    return made


class TestTheTranslationKeepsTheRealCash:

    def test_buying_the_twin_spends_money(self):
        """
        Продаём «ДА» по 0.20 — на бирже это покупка «НЕТ» по 0.80. Пять
        контрактов забирают $4.00, а не приносят $1.00.
        """
        got = mm.as_our_side(
            [{'token': 'NO', 'side': 'bid', 'price': 0.80, 'size': 5.0,
              'id': '1'}], MARKETS)
        assert got[0]['token'] == 'YES'
        assert got[0]['side'] == 'ask'
        assert got[0]['price'] == pytest.approx(0.20)
        assert got[0]['cash'] == pytest.approx(-4.0)

    def test_selling_the_twin_brings_money(self):
        """Обратный случай: продали «НЕТ» по 0.80 — получили $4.00."""
        got = mm.as_our_side(
            [{'token': 'NO', 'side': 'ask', 'price': 0.80, 'size': 5.0,
              'id': '1'}], MARKETS)
        assert got[0]['side'] == 'bid'
        assert got[0]['cash'] == pytest.approx(4.0)

    def test_an_ordinary_trade_carries_no_override(self):
        """Обычная сделка своим токеном считается как считалась."""
        got = mm.as_our_side(
            [{'token': 'YES', 'side': 'bid', 'price': 0.20, 'size': 5.0,
              'id': '1'}], MARKETS)
        assert 'cash' not in got[0]

    def test_the_engine_takes_the_real_cash(self, tmp_path):
        maker = _maker(100.0, tmp_path)
        maker.apply_exchange_trades(mm.as_our_side(
            [{'token': 'NO', 'side': 'bid', 'price': 0.80, 'size': 5.0,
              'id': '1'}], MARKETS))
        assert maker.state['cash'] == pytest.approx(96.0)
        assert maker.state['books']['YES']['position'] == pytest.approx(-5.0)

    def test_the_old_way_was_wrong_by_a_dollar_a_contract(self, tmp_path):
        """
        Прежний счёт прибавил бы $1.00 вместо списания $4.00 — разница ровно
        пять долларов на пять контрактов.
        """
        maker = _maker(100.0, tmp_path)
        maker.apply_exchange_trades(
            [{'token': 'YES', 'side': 'ask', 'price': 0.20, 'size': 5.0,
              'id': '1'}])
        assert maker.state['cash'] == pytest.approx(101.0)


class TestNegativeIsTheOtherContract:

    def test_a_short_is_valued_as_the_twin(self, tmp_path):
        """
        Минус пять «ДА» по цене 0.20 — это плюс пять «НЕТ» по 0.80, то есть
        актив на $4.00, а не долг на $1.00.
        """
        maker = _maker(0.0, tmp_path)
        maker._slot('YES')['position'] = -5.0
        maker._slot('YES')['avg_cost'] = 0.20
        assert maker.mark_to_market({'YES': 0.20})['inventory'] == pytest.approx(4.0)

    def test_money_and_stock_add_up_to_zero(self, tmp_path):
        """
        Купили встречный на $4.00 — денег на четыре меньше, запаса на четыре
        больше. Капитал не изменился, и это единственная верная проверка:
        обе половины считаются по-разному и обязаны сойтись.
        """
        maker = _maker(100.0, tmp_path)
        maker.apply_exchange_trades(mm.as_our_side(
            [{'token': 'NO', 'side': 'bid', 'price': 0.80, 'size': 5.0,
              'id': '1'}], MARKETS))
        assert maker.mark_to_market({'YES': 0.20})['equity'] == pytest.approx(100.0)

    def test_a_long_is_valued_as_before(self, tmp_path):
        maker = _maker(0.0, tmp_path)
        maker._slot('YES')['position'] = 5.0
        maker._slot('YES')['avg_cost'] = 0.20
        assert maker.mark_to_market({'YES': 0.20})['inventory'] == pytest.approx(1.0)

    def test_exposure_counts_the_twin_at_its_price(self, tmp_path):
        """
        Предел вложенного считался по дешёвой стороне и пропускал впятеро
        больше разрешённого.
        """
        maker = _maker(0.0, tmp_path)
        maker._slot('YES')['position'] = -5.0
        maker._slot('YES')['avg_cost'] = 0.20
        assert maker.exposure({'YES': 0.20}) == pytest.approx(4.0)
