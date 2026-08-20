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

    def test_the_message_distinguishes_the_two_cases(self):
        """
        «Программный SL активен» — правда только при рыночном входе. При
        лимитном стоп уже стоит на бирже, и та же фраза вводила бы в
        заблуждение ровно там, где важна точность.
        """
        assert 'SL не уточнён' in self.SRC
