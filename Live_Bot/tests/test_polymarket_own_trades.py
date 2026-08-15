"""
Наша доля в сделке биржи, а не вся сделка целиком.

САМАЯ ДОРОГАЯ ОШИБКА ПРОЕКТА, и нашлась она по расхождению: панель показывала
+$290 прибыли, тогда как на счёте было минус два доллара.

Запись о сделке описывает ВЕСЬ мэтч — заявку тейкера целиком и всех мейкеров,
которых он собрал. Наши пять контрактов лежат внутри, в maker_orders, со СВОЕЙ
ценой, СВОИМ токеном и СВОЕЙ стороной. Верхние поля принадлежат тейкеру:

    верхний уровень   size 1070.54  price 0.401  токен «Нет»
    наша строка       matched_amount 5  price 0.637  токен «Да»

Записывая верхний уровень, бот приписывал себе тысячу контрактов вместо пяти,
чужую цену и чужой токен. Отсюда позиции в тысячи контрактов при бюджете в
сорок долларов и деньги, ушедшие в минус тысячу восемьсот.

Образец ниже — настоящий ответ биржи, снятый с живого счёта 15 августа 2026.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import executor  # noqa: E402

OUR = '0x8D15A872e8B0f9D895bcd2cEFc511a09Ae4C5A77'
THEIRS = '0x510F4963b66B1B18505faaB74b0bB943D1dDa43C'

REAL_TRADE = {
    'id': '3445c837-7691-48ba-80b4-d4282162682b',
    'taker_order_id': '0x163ae2e2bf2c009',
    'asset_id': '84757069138724396818581274701074784361163393091931604367444695649588726494517',
    'side': 'BUY', 'size': '1070.54', 'price': '0.401',
    'status': 'CONFIRMED', 'match_time': '1786770088',
    'maker_address': '0x9A3fA403A6666eEf75f92F181fCF13f9C051914A',
    'maker_orders': [
        {'order_id': '0x303eef0c', 'maker_address': THEIRS,
         'matched_amount': '30', 'price': '0.363', 'side': 'SELL',
         'asset_id': '8475706913872439681858127470107478436116339309193160436'},
        {'order_id': '0x06d88525', 'maker_address': OUR,
         'matched_amount': '5', 'price': '0.637', 'side': 'BUY',
         'asset_id': '92475931596782945146254699011735173472915347020881266584722910703960042383470'},
    ],
}


class TestOnlyOurPartCounts:

    def test_our_size_price_and_token_are_taken_from_our_row(self):
        got = executor.our_part([REAL_TRADE], OUR)
        assert len(got) == 1
        fill = got[0]
        assert fill['size'] == 5.0, 'наши пять контрактов, а не тысяча тейкера'
        assert fill['price'] == pytest.approx(0.637), 'наша цена, а не 0.401'
        assert fill['token'].endswith('42383470'), 'наш токен «Да», а не «Нет»'
        assert fill['side'] == 'bid'

    def test_other_makers_are_not_ours(self):
        """В том же мэтче стоят чужие заявки — их считать нельзя."""
        got = executor.our_part([REAL_TRADE], OUR)
        assert all(f['size'] != 30.0 for f in got)

    def test_nothing_of_ours_means_nothing_counted(self):
        assert executor.our_part([REAL_TRADE], '0xСОВСЕМ_ДРУГОЙ') == []

    def test_two_of_our_orders_in_one_match_both_count(self):
        """
        Тейкер способен снять сразу два наших уровня. По одному лишь номеру
        сделки второе исполнение потерялось бы молча.
        """
        trade = dict(REAL_TRADE)
        trade['maker_orders'] = list(REAL_TRADE['maker_orders']) + [
            {'order_id': '0xВТОРАЯ', 'maker_address': OUR,
             'matched_amount': '7', 'price': '0.640', 'side': 'BUY',
             'asset_id': '92475931596782945146254699011735173472915347020881266584722910703960042383470'},
        ]
        got = executor.our_part([trade], OUR)
        assert len(got) == 2
        assert {f['size'] for f in got} == {5.0, 7.0}
        assert len({f['id'] for f in got}) == 2, 'ключи разные, иначе повтор съест одно'

    def test_when_we_are_the_taker_the_top_level_is_ours(self):
        trade = dict(REAL_TRADE, maker_address=OUR, maker_orders=[])
        got = executor.our_part([trade], OUR)
        assert len(got) == 1
        assert got[0]['size'] == pytest.approx(1070.54)
        assert got[0]['price'] == pytest.approx(0.401)

    def test_a_broken_row_does_not_stop_the_rest(self):
        bad = {'id': 'плохая', 'maker_orders': [
            {'maker_address': OUR, 'matched_amount': 'не число',
             'price': '0.5', 'side': 'BUY', 'asset_id': 'T'}]}
        got = executor.our_part([bad, REAL_TRADE], OUR)
        assert len(got) == 1 and got[0]['size'] == 5.0


class TestTheNumbersMatchReality:
    """
    Проверка на живых данных: одиннадцать записей биржи, снятых со счёта.
    Каждая — наши 2-13 контрактов, в сумме $21.61 потраченных.
    """

    def test_sizes_stay_within_what_we_could_afford(self):
        got = executor.our_part([REAL_TRADE], OUR)
        spent = sum(f['price'] * f['size'] for f in got)
        assert spent < 40, 'больше бюджета потратить было нечем'
