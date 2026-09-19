"""
Предел запросов биржи — повод повторить, а не пропустить пару.

19 сентября 2026 в первые полминуты каждого часа Bybit отвечал
«Too many visits» (retCode 10006): 21 отказ за три часа, и каждый — пара,
которую сканер в этот цикл не увидел. Загрузка свечей теперь повторяет
запрос с нарастающей паузой; прочие сетевые ошибки идут наверх как раньше.
"""

import os
import sys

import ccxt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exchange  # noqa: E402


class Flaky:
    def __init__(self, failures, error):
        self.failures = failures
        self.error = error
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return [[1, 1, 1, 1, 1, 1]]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, 'sleep', lambda s: None)


def test_rate_limit_is_retried():
    ex = Flaky(2, ccxt.RateLimitExceeded('bybit {"retCode":10006}'))
    assert exchange._fetch_with_backoff(ex, 'BTCUSDT', '1h', None, 10) == [[1, 1, 1, 1, 1, 1]]
    assert ex.calls == 3


def test_bybit_code_in_a_plain_exchange_error_is_retried_too():
    ex = Flaky(1, ccxt.NetworkError('bybit {"retCode":10006,"retMsg":"Too many visits"}'))
    assert exchange._fetch_with_backoff(ex, 'BTCUSDT', '1h', None, 10)
    assert ex.calls == 2


def test_other_network_errors_are_not_retried():
    ex = Flaky(1, ccxt.NetworkError('timeout'))
    with pytest.raises(ccxt.NetworkError):
        exchange._fetch_with_backoff(ex, 'BTCUSDT', '1h', None, 10)
    assert ex.calls == 1


def test_a_persistent_limit_still_fails_after_the_retries():
    ex = Flaky(99, ccxt.RateLimitExceeded('10006'))
    with pytest.raises(ccxt.RateLimitExceeded):
        exchange._fetch_with_backoff(ex, 'BTCUSDT', '1h', None, 10)
    assert ex.calls == len(exchange.RATE_LIMIT_RETRIES) + 1
