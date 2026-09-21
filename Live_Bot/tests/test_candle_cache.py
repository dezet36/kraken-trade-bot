"""
Свечи — сырые данные общего слоя: за цикл берутся с биржи один раз и
раздаются всем читателям копиями.

До 21.09.2026 одни и те же свечи за проход просили четыре-пять раз (SMC,
ИИ, уровни, Боллинджер — каждый себе): ~75 запросов за цикл на 15 пар.
Кэш живёт меньше цикла, чтобы следующий проход видел свежую формирующуюся
свечу; выборки из прошлого (since) идут мимо кэша.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exchange  # noqa: E402


class Counting:
    id = 'fake'

    def __init__(self, n=600):
        self.calls = []
        self.n = n

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append((symbol, timeframe, since, limit))
        limit = limit or self.n
        base = 1_700_000_000_000
        return [[base + i * 60_000, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(limit)]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    exchange.clear_candle_cache()
    monkeypatch.setattr(exchange, 'market_symbol', lambda pair, client: pair)
    yield
    exchange.clear_candle_cache()


class TestOneRequestPerCycle:

    def test_the_second_reader_gets_the_same_candles_without_a_request(self):
        ex = Counting()
        a = exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        b = exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        assert len(ex.calls) == 1
        assert a.equals(b)

    def test_a_smaller_window_is_cut_from_the_cached_one(self):
        ex = Counting()
        big = exchange.fetch_ohlcv('1h', limit=500, symbol='BTCUSDT', client=ex)
        small = exchange.fetch_ohlcv('1h', limit=305, symbol='BTCUSDT', client=ex)
        assert len(ex.calls) == 1
        assert len(small) == 305
        assert small.iloc[-1]['timestamp'] == big.iloc[-1]['timestamp'], 'хвост тот же — последняя свеча одна'
        assert list(small.index) == list(range(305))

    def test_a_bigger_window_goes_to_the_exchange(self):
        ex = Counting()
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        exchange.fetch_ohlcv('1h', limit=500, symbol='BTCUSDT', client=ex)
        assert len(ex.calls) == 2

    def test_pairs_timeframes_and_exchanges_do_not_mix(self):
        ex = Counting()
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        exchange.fetch_ohlcv('4h', limit=300, symbol='BTCUSDT', client=ex)
        exchange.fetch_ohlcv('1h', limit=300, symbol='ETHUSDT', client=ex)
        other = Counting()
        other.id = 'other'
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=other)
        assert len(ex.calls) == 3 and len(other.calls) == 1


class TestReadersCannotPoisonEachOther:

    def test_each_reader_gets_its_own_copy(self):
        ex = Counting()
        a = exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        a['ema'] = 1.0
        a.loc[0, 'close'] = -1.0
        b = exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        assert 'ema' not in b.columns
        assert b.iloc[0]['close'] == 1.5


class TestTheCacheOutlivesNothing:

    def test_it_expires_before_the_next_cycle(self, monkeypatch):
        import time
        ex = Counting()
        t = [1000.0]
        monkeypatch.setattr(time, 'monotonic', lambda: t[0])
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        t[0] += exchange.CANDLE_CACHE_TTL_S - 1
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        assert len(ex.calls) == 1
        t[0] += 2
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        assert len(ex.calls) == 2

    def test_the_ttl_is_shorter_than_the_scan_cycle(self):
        # Цикл сканера — 5 минут (bot.py: trading_cycle, interval, minutes=5).
        assert exchange.CANDLE_CACHE_TTL_S < 5 * 60, 'иначе следующий цикл увидит старую свечу'

    def test_history_requests_bypass_the_cache(self):
        ex = Counting()
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex)
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex, since=1_690_000_000_000)
        exchange.fetch_ohlcv('1h', limit=300, symbol='BTCUSDT', client=ex, since=1_690_000_000_000)
        assert len(ex.calls) == 3

    def test_an_empty_answer_is_not_cached(self):
        ex = Counting(n=3)
        assert exchange.fetch_ohlcv('1h', limit=3, symbol='BTCUSDT', client=ex) is None
        assert exchange.fetch_ohlcv('1h', limit=3, symbol='BTCUSDT', client=ex) is None
        assert len(ex.calls) == 2
