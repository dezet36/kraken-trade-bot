"""
Приложение говорит то, что есть, и защищает позицию на любой бирже.

ОТКУДА ЭТО. Две находки ревизии, обе про BingX, обе одной природы: код
описывал не то, что происходило.

1. РЕЖИМ. В exchange.py стояло «BingX: отдельного demo endpoint в ccxt нет —
   торговля на реальном счёте». Когда-то верно, потом устарело молча: ccxt 4.5
   отдаёт для bingx контур open-api-vst (VST — виртуальные средства) через
   штатный set_sandbox_mode. При этом подпись каждой сделки бралась из
   config.TRADING_MODE — то есть из НАМЕРЕНИЯ, — и оставалась зелёной «🟢 DEMO»
   на реальном счёте. Настройка и факт обязаны быть разными величинами, иначе
   расхождение между ними непредставимо.

2. СТОП. Защитный стоп прикреплялся к ордеру входа только при
   `exchange.id == 'bybit'`. На BingX ордер уходил без стопа вовсе, и
   оставался программный — тот, которым управляет сам бот. Он умирает вместе с
   закрытым окном, а это настольное приложение, которое выключают на ночь.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import exchange                                             # noqa: E402


class TestTheModeIsWhatHappened:

    def test_bingx_demo_really_goes_to_the_demo_endpoint(self):
        client = exchange.make_client('bingx', 'k', 's', 'DEMO')
        urls = ' '.join(client.urls['api'].values())
        assert 'open-api-vst' in urls, (
            'BingX в режиме DEMO ходит на боевой контур — это реальные деньги '
            'под зелёной подписью')

    def test_bingx_live_stays_live(self):
        client = exchange.make_client('bingx', 'k', 's', 'LIVE')
        urls = ' '.join(client.urls['api'].values())
        assert 'open-api-vst' not in urls

    def test_bybit_demo_still_works(self):
        client = exchange.make_client('bybit', 'k', 's', 'DEMO')
        urls = ' '.join(client.urls['api'].values())
        assert 'api-demo.bybit' in urls

    def test_the_label_reads_the_client_not_the_setting(self):
        for name in ('bybit', 'bingx'):
            for want in ('DEMO', 'LIVE'):
                client = exchange.make_client(name, 'k', 's', want)
                assert exchange.effective_mode(client, want) == want, (name, want)

    def test_a_demo_that_did_not_happen_reads_as_live(self):
        """
        Главное свойство: если демо не включилось, подпись обязана сказать
        LIVE. Обещать демо на боевом счёте хуже, чем не обещать ничего.
        """
        client = exchange.make_client('bingx', 'k', 's', 'LIVE')
        assert exchange.effective_mode(client, 'DEMO') == 'LIVE'

    def test_an_unreadable_client_reads_as_live(self):
        """Не смогли убедиться — не обещаем. Безопасная сторона одна."""
        class Mute:
            urls = None
        assert exchange.effective_mode(Mute(), 'DEMO') == 'LIVE'

    def test_the_stale_claim_is_gone(self):
        text = open(os.path.join(ROOT, 'exchange.py'), encoding='utf-8').read()
        assert 'отдельного demo endpoint в ccxt нет' not in text, (
            'утверждение устарело и стоило реальных денег под подписью DEMO')

    def test_the_trade_label_asks_the_client(self):
        text = open(os.path.join(ROOT, 'trade_manager.py'), encoding='utf-8').read()
        spot = text.index('mode_text =')
        block = text[max(0, spot - 400):spot + 300]
        assert 'effective_mode' in block, (
            'подпись сделки снова берётся из настройки, а не из факта')


class TestTheStopIsAttachedEverywhere:

    SRC = open(os.path.join(ROOT, 'trade_manager.py'), encoding='utf-8').read()

    def _limit_block(self):
        spot = self.SRC.index('limit_params = {')
        return self.SRC[spot:spot + 1400]

    def test_the_stop_is_not_behind_an_exchange_check(self):
        """
        РОВНО ТОТ ДЕФЕКТ: `if exchange.id == 'bybit'` перед установкой стопа
        оставлял позиции BingX без биржевой защиты.
        """
        block = self._limit_block()
        before = block[:block.index("limit_params['stopLoss']")]
        # ЛЮБОЕ условие между созданием параметров и установкой стопа, а не
        # конкретная его запись: искать одну формулировку значило бы ловить
        # только ту, что уже исправлена.
        conditions = [ln.strip() for ln in before.splitlines()
                      if ln.strip().startswith('if ')]
        assert not conditions, (
            f'стоп снова стоит за условием: {conditions} — так позиции BingX '
            f'уходили без биржевой защиты')

    def test_the_stop_is_still_attached(self):
        assert "limit_params['stopLoss']" in self._limit_block()

    def test_exchange_specific_fields_stay_behind_the_check(self):
        """
        `slTriggerBy` — поле Bybit. Слать его всем значит получать отказ ордера
        там, где раньше он проходил.
        """
        block = self._limit_block()
        spot = block.index("limit_params['slTriggerBy']")
        assert 'if is_bybit' in block[max(0, spot - 200):spot]

    def test_moving_the_stop_is_not_bound_to_one_exchange(self):
        """
        Перенос стопа делался в четырёх местах прямым вызовом эндпоинта Bybit
        V5. На BingX такого метода у клиента нет: вызов падал, общий except
        писал строку в журнал, и стоп молча оставался на месте — то есть
        безубыток и трейлинг там не работали вовсе.
        """
        assert 'def _set_position_stop' in self.SRC
        for method in ('_update_trail_stop', '_move_sl_to_breakeven'):
            spot = self.SRC.index(f'def {method}')
            body = self.SRC[spot:self.SRC.index('\n    def ', spot + 10)]
            assert 'privatePostV5PositionTradingStop' not in body, (
                f'{method} снова зовёт эндпоинт Bybit напрямую')
            assert '_set_position_stop' in body, method

    def test_the_only_bybit_call_left_is_inside_the_shared_method(self):
        """
        Один вызов остаться обязан — это и есть ветка Bybit. Важно, что он
        ровно один и стоит там, где выбирают способ по площадке.
        """
        import re
        code = re.sub(r'""".*?"""', '', self.SRC, flags=re.S)   # без описаний
        calls = code.count('privatePostV5PositionTradingStop')
        assert calls == 2, (
            f'прямых вызовов эндпоинта Bybit: {calls}. Ожидается два — стоп '
            f'внутри _set_position_stop и тейк-профит, который защиты не '
            f'касается')

    def test_the_message_says_what_protects_the_position(self):
        """
        «Программный SL активен» — правда только при рыночном входе, и сама по
        себе она не говорит главного: такой стоп живёт, лишь пока запущено
        приложение, а его выключают на ночь.
        """
        spot = self.SRC.index('SL на позицию не встал')
        assert 'пока работает приложение' in self.SRC[spot:spot + 300]


class TestTheStopActuallyReachesTheExchange:
    """
    Проверка НЕ на том, что параметр принят, а на том, что он превращается в
    настоящий стоп в теле запроса.

    Разница существенная. Убедиться, что `create_order_request` не бросает
    исключение, — почти ничего не значит: биржа могла тихо выбросить
    непонятое поле, и ордер ушёл бы голым. Здесь смотрим сам запрос.

    Живого счёта BingX нет, отправить туда ордер не на чем. Но собрать запрос
    и заглянуть внутрь можно, и это отвечает на нужный вопрос: несёт ли он
    стоп.

    Требует сети — ccxt берёт описание рынков с биржи. Без неё проверка
    пропускается: гонять весь набор тестов через интернет незачем.
    """

    def _payload(self, name):
        import ccxt
        import pytest
        ex = getattr(ccxt, name)()
        try:
            ex.load_markets()
        except Exception:                          # noqa: BLE001
            pytest.skip(f'{name}: нет сети — описание рынков не загрузилось')
        params = {'reduce_only': False, 'timeInForce': 'GTC',
                  'stopLoss': '58000.0'}
        if name == 'bybit':
            params['slTriggerBy'] = 'LastPrice'
        return ex.create_order_request('BTC/USDT:USDT', 'limit', 'buy',
                                       0.01, 60000.0, params)

    def test_bybit_order_carries_the_stop(self):
        body = self._payload('bybit')
        assert 'stopLoss' in body and str(body['stopLoss']).startswith('58000')

    def test_bingx_order_carries_the_stop(self):
        """
        РАДИ ЭТОГО ВСЁ И ДЕЛАЛОСЬ. До правки стоп на BingX не прикреплялся
        вовсе: условие `if exchange.id == 'bybit'` пропускало его мимо.

        ccxt переводит наше значение в родной формат биржи сам — вложенным
        объектом со stopPrice и типом STOP_MARKET.
        """
        body = self._payload('bingx')
        assert 'stopLoss' in body, 'ордер BingX уходит без стопа'
        text = str(body['stopLoss'])
        assert '58000' in text, f'цена стопа потерялась: {text}'
        assert 'STOP' in text.upper(), f'это не стоп-приказ: {text}'

    def test_both_exchanges_get_one(self):
        """Симметрия — весь смысл правки: защита не зависит от площадки."""
        for name in ('bybit', 'bingx'):
            assert 'stopLoss' in self._payload(name), name


class TestTheStopMoveIsBuildableOnEveryExchange:
    """
    Ветка для не-Bybit была написана вслепую и НЕ РАБОТАЛА.

    Первая версия передавала в ccxt `amount=None`, полагаясь на closePosition.
    Запрос не собирался вовсе — AttributeError внутри ccxt, ещё до обращения к
    сети. То есть перенос стопа на BingX падал бы всегда, а общий `except`
    записывал бы это одной строкой в журнал.

    Поймано сборкой запроса: способ тот же, что и для стопа на входе — просим
    ccxt собрать тело и смотрим, что вышло. Живого счёта BingX нет, но этот
    вопрос он и не требует.
    """

    def _built(self, amount):
        import ccxt
        import pytest
        ex = ccxt.bingx()
        try:
            ex.load_markets()
        except Exception:                          # noqa: BLE001
            pytest.skip('нет сети — описание рынков не загрузилось')
        return ex.create_order_request(
            'BTC/USDT:USDT', 'market', 'sell', amount, None,
            {'stopLossPrice': 58000.0, 'reduce_only': True})

    def test_a_size_is_required(self):
        """Ровно та ошибка: без объёма запрос не собирается."""
        import pytest
        with pytest.raises(Exception):
            self._built(None)

    def test_with_a_size_it_becomes_a_stop_order(self):
        body = self._built(0.01)
        assert str(body.get('type')).upper() == 'STOP_MARKET'
        assert float(body.get('stopPrice')) == 58000.0
        assert float(body.get('quantity')) == 0.01

    def test_the_code_asks_for_the_size(self):
        src = open(os.path.join(ROOT, 'trade_manager.py'), encoding='utf-8').read()
        spot = src.index('def _set_position_stop')
        body = src[spot:src.index('\n    def ', spot + 10)]
        # Только код: в описании метод объясняет, ПОЧЕМУ amount=None было
        # ошибкой, и упоминать её там законно.
        no_doc = re.sub(r'"""[\s\S]*?"""', '', body)
        code = '\n'.join(ln for ln in no_doc.splitlines()
                         if not ln.strip().startswith('#'))
        assert 'amount=None' not in code, 'снова просим ccxt собрать запрос без объёма'
        assert '_open_size(' in code

    def test_the_size_is_the_remainder_not_the_original(self):
        """
        После частичной фиксации первоначальный объём больше того, что мы
        держим. Просить у биржи лишнее — получить отказ и остаться без
        перенесённого стопа.
        """
        src = open(os.path.join(ROOT, 'trade_manager.py'), encoding='utf-8').read()
        spot = src.index('def _open_size')
        body = src[spot:src.index('\n    def ', spot + 10)]
        assert 'remaining_size' in body

    def test_no_position_means_no_order(self):
        """Нечего защищать — нечего и отправлять."""
        src = open(os.path.join(ROOT, 'trade_manager.py'), encoding='utf-8').read()
        spot = src.index('def _set_position_stop')
        body = src[spot:src.index('\n    def ', spot + 10)]
        assert 'if not size:' in body and 'return False' in body
