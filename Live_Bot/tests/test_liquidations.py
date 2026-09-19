"""
Сборщик ликвидаций: что пишет, как читает и как это видит модель.

Сети здесь нет: проверяется разбор сообщения биржи, файл и чтение окна.
Соединение проверяется только живым запуском на сервере.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import liquidations
import llm_context
import llm_market


@pytest.fixture(autouse=True)
def _clean():
    with liquidations._lock:
        liquidations._buffer.clear()
        liquidations._wanted.clear()
        liquidations._wanted.update({'BTCUSDT': 'BTCUSDT', 'SHIB1000USDT': 'SHIB1000USDT'})
        liquidations._stats['events'] = 0
    yield
    with liquidations._lock:
        liquidations._buffer.clear()
        liquidations._wanted.clear()


class TestTheMessageIsUnderstood:

    def test_a_liquidation_becomes_a_row(self):
        liquidations._handle(json.dumps({
            'topic': 'allLiquidation.BTCUSDT', 'type': 'snapshot', 'ts': 1_700_000_000_000,
            'data': [{'T': 1_700_000_000_123, 's': 'BTCUSDT', 'S': 'Buy',
                      'v': '0.5', 'p': '81250.5'}]}))
        with liquidations._lock:
            rows = list(liquidations._buffer)
        assert rows == [{'ts': 1_700_000_000_123, 'pair': 'BTCUSDT', 'side': 'long',
                         'price': 81250.5, 'size': 0.5}]

    def test_sell_means_a_short_was_liquidated(self):
        liquidations._handle(json.dumps({
            'topic': 'allLiquidation.BTCUSDT',
            'data': [{'T': 1, 's': 'BTCUSDT', 'S': 'Sell', 'v': '1', 'p': '100'}]}))
        with liquidations._lock:
            assert liquidations._buffer[0]['side'] == 'short'

    def test_other_topics_and_garbage_are_ignored(self):
        liquidations._handle('{"op":"pong"}')
        liquidations._handle('не json')
        liquidations._handle(json.dumps({'topic': 'tickers.BTCUSDT', 'data': {}}))
        with liquidations._lock:
            assert liquidations._buffer == []

    def test_the_exchange_id_maps_back_to_our_pair(self):
        with liquidations._lock:
            liquidations._wanted['SHIB1000USDT'] = '1000SHIBUSDT'
        liquidations._handle(json.dumps({
            'topic': 'allLiquidation.1000SHIBUSDT',
            'data': [{'T': 1, 's': '1000SHIBUSDT', 'S': 'Buy', 'v': '1', 'p': '0.005'}]}))
        with liquidations._lock:
            assert liquidations._buffer[0]['pair'] == 'SHIB1000USDT'


class TestTheFile:

    def test_flush_appends_and_rows_read_a_window(self, tmp_path, monkeypatch):
        path = tmp_path / 'liq.jsonl'
        monkeypatch.setattr(liquidations, 'PATH', str(path))
        for ts in (100, 200, 300):
            liquidations._handle(json.dumps({
                'topic': 'allLiquidation.BTCUSDT',
                'data': [{'T': ts, 's': 'BTCUSDT', 'S': 'Buy', 'v': '1', 'p': '100'}]}))
        liquidations._flush()
        liquidations._flush()                          # пустой буфер — ничего не дописывает
        assert len(path.read_text(encoding='utf-8').splitlines()) == 3

        assert [r['ts'] for r in liquidations.rows('BTCUSDT', since=150, upto=250)] == [200]
        assert liquidations.rows('ETHUSDT') == []
        assert liquidations.first_ts() == 100

    def test_no_file_means_no_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr(liquidations, 'PATH', str(tmp_path / 'нет.jsonl'))
        assert liquidations.rows('BTCUSDT') == []
        assert liquidations.first_ts() is None


class TestWhatTheModelSees:

    def _rows(self, now):
        return [
            {'ts': now - 3_600_000 * 2, 'pair': 'BTCUSDT', 'side': 'long', 'price': 99.0, 'size': 5.0},
            {'ts': now - 3_600_000 * 2 + 1000, 'pair': 'BTCUSDT', 'side': 'long', 'price': 99.2, 'size': 3.0},
            {'ts': now - 60_000, 'pair': 'BTCUSDT', 'side': 'short', 'price': 102.0, 'size': 1.0},
        ]

    def test_totals_and_clusters(self, monkeypatch):
        now = 1_700_000_000_000
        monkeypatch.setattr(liquidations, 'first_ts', lambda path=None: now - 30 * 3_600_000)
        monkeypatch.setattr(liquidations, 'rows',
                            lambda pair, since=None, upto=None, path=None: self._rows(now))
        out = llm_market.liquidation_facts('BTCUSDT', 100.0, upto=now)
        assert out['h24']['long_size'] == 8.0 and out['h24']['short_n'] == 1
        assert out['h4']['long_n'] == 2
        biggest = max(out['clusters'], key=lambda c: c['share_pct'])
        assert biggest['from'] == 99.0 and biggest['to'] == 99.2
        assert biggest['mostly'] == 'лонги'
        assert biggest['share_pct'] == pytest.approx(8 / 9 * 100)

    def test_a_young_history_says_so(self, monkeypatch):
        now = 1_700_000_000_000
        monkeypatch.setattr(liquidations, 'first_ts', lambda path=None: now - 20 * 60_000)
        monkeypatch.setattr(liquidations, 'rows',
                            lambda pair, since=None, upto=None, path=None: [])
        out = llm_market.liquidation_facts('BTCUSDT', 100.0, upto=now)
        assert out['young_min'] == 20
        text = '\n'.join(llm_context._liq_fact_lines(out))
        assert 'сбор идёт 20 мин' in text

    def test_no_collector_is_not_measured(self, monkeypatch):
        monkeypatch.setattr(liquidations, 'first_ts', lambda path=None: None)
        assert llm_market.liquidation_facts('BTCUSDT', 100.0) is None
        assert llm_context._liq_fact_lines(None) == ['  —']

    def test_the_block_is_rendered(self, monkeypatch):
        now = 1_700_000_000_000
        monkeypatch.setattr(liquidations, 'first_ts', lambda path=None: now - 30 * 3_600_000)
        monkeypatch.setattr(liquidations, 'rows',
                            lambda pair, since=None, upto=None, path=None: self._rows(now))
        out = llm_market.liquidation_facts('BTCUSDT', 100.0, upto=now)
        text = '\n'.join(llm_context._liq_fact_lines(out))
        assert 'За 24ч: лонгов 2 (объём 8), шортов 1 (объём 1)' in text
        assert '99..99.2 (лонги, -0.9%, 89%)' in text
