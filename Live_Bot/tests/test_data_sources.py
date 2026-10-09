"""
Реестр источников данных (data/sources.py) — реорганизация, этап 3.

Проверяется, что
  - по умолчанию сборщики спрашивают РОВНО те же адреса, что до реестра
    (запросы сравниваются строкой целиком);
  - оператор меняет адрес, запасные адреса и прокси файлом data_sources.json,
    а битый файл торговлю не останавливает;
  - к запасному адресу идём при сбое сети или 5xx, но не при ответе «нет такого»;
  - здоровье источника видно (успех, отказ, задержка), пароль прокси на панель
    не попадает;
  - рыночный клиент ccxt проходит через реестр, не меняя адресов без правки.
"""

import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import sources  # noqa: E402

H = 3_600_000


@pytest.fixture(autouse=True)
def clean_health(monkeypatch):
    monkeypatch.setattr(sources, '_health', {})
    monkeypatch.setattr(sources, '_file', {'key': None, 'data': {}})


@pytest.fixture()
def calls(monkeypatch):
    """Подмена сети: запоминает адреса, отвечает по таблице или отказом."""
    seen = []
    answers = {}

    def fake(full_url, timeout, proxy_url=None):
        seen.append((full_url, timeout, proxy_url))
        for prefix, answer in answers.items():
            if full_url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise urllib.error.URLError('нет сети в проверке')
    monkeypatch.setattr(sources, '_http_json', fake)
    return seen, answers


def write_settings(data):
    with open(sources.settings_path(), 'w', encoding='utf-8') as fh:
        json.dump(data, fh)


# ── Те же адреса, что до реестра ────────────────────────────────────────────

class TestSameRequestsAsBefore:
    def test_flow_data(self, calls):
        from data import flow_data
        seen, answers = calls
        answers['https://fapi.binance.com'] = []
        answers['https://api.bybit.com'] = {'result': {'list': []}}
        start, end = 1_700_000_000_000 // H * H, 1_700_000_000_000 // H * H + 5 * H
        flow_data._binance('SHIB1000USDT', start, end)
        flow_data._bybit_ratio('ETHUSDT', start, end)
        flow_data._bybit_back(lambda e: f'/v5/market/open-interest?category=linear&symbol=ETHUSDT'
                                        f'&intervalTime=1h&limit=200&endTime={e}',
                              'timestamp', 'openInterest', start, end)
        urls = [u for u, _, _ in seen]
        assert urls == [
            f'https://fapi.binance.com/fapi/v1/klines?symbol=1000SHIBUSDT&interval=1h'
            f'&startTime={start}&endTime={end + H - 1}&limit=1500',
            f'https://api.bybit.com/v5/market/account-ratio?category=linear&symbol=ETHUSDT&period=1h'
            f'&limit=500&startTime={start}&endTime={end}',
            f'https://api.bybit.com/v5/market/open-interest?category=linear&symbol=ETHUSDT'
            f'&intervalTime=1h&limit=200&endTime={end}',
        ]
        assert all(t == 20 and p is None for _, t, p in seen)

    def test_market_mood(self, calls):
        from data import market_mood
        seen, _ = calls
        t_ms = 1_700_000_000_000 // H * H
        market_mood._fetch(t_ms)            # все отказали — прочерки, без исключений
        hosts = [u.split('/')[2] for u, _, _ in seen]
        assert hosts == ['www.deribit.com', 'api.exchange.coinbase.com', 'api.binance.com',
                         'fapi.binance.com']
        assert seen[2][0] == (f'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h'
                              f'&endTime={t_ms + H - 1}&limit=30')

    def test_market_cap_supply(self, calls):
        from data import market_cap
        seen, answers = calls
        answers['https://api.coingecko.com'] = []
        with pytest.raises(RuntimeError):        # пустой ответ — отказ, как и раньше
            market_cap._fetch_supply()
        assert seen[0][0] == ('https://api.coingecko.com/api/v3/coins/markets'
                              f'?vs_currency=usd&order=market_cap_desc&per_page={market_cap.TOP}&page=1')

    def test_news(self, calls):
        from data import news_feed
        seen, answers = calls
        answers['https://api.bybit.com'] = {'result': {'list': []}}
        news_feed._last_poll['ts'] = 0
        news_feed.poll(now_s=10 ** 9, every_s=0)
        assert seen[0][0] == 'https://api.bybit.com/v5/announcements/index?locale=en-US&limit=20'

    def test_streams_keep_their_address(self):
        assert sources.urls('bybit_ws') == (
            os.getenv('BYBIT_WS_PUBLIC', 'wss://stream.bybit.com/v5/public/linear'),)
        assert sources.proxy('bybit_ws') is None


# ── Правка оператора ────────────────────────────────────────────────────────

class TestOperatorSettings:
    def test_file_changes_address_fallbacks_proxy_timeout(self, calls):
        write_settings({'coingecko': {'url': 'https://cg.example/', 'fallbacks': ['https://cg2.example'],
                                      'proxy': 'http://u:p@10.0.0.5:3128', 'timeout': 7}})
        assert sources.urls('coingecko') == ('https://cg.example', 'https://cg2.example')
        seen, answers = calls
        answers['https://cg2.example'] = ['ok']
        assert sources.get('coingecko', '/x') == ['ok']
        assert seen == [('https://cg.example/x', 7.0, 'http://u:p@10.0.0.5:3128'),
                        ('https://cg2.example/x', 7.0, 'http://u:p@10.0.0.5:3128')]

    def test_other_sources_untouched(self):
        write_settings({'coingecko': {'url': 'https://cg.example'}})
        assert sources.url('bybit') == 'https://api.bybit.com'
        assert sources.source('bybit')['overridden'] is False

    def test_broken_file_means_defaults(self):
        with open(sources.settings_path(), 'w', encoding='utf-8') as fh:
            fh.write('{ это не json')
        assert sources.url('deribit') == 'https://www.deribit.com'


# ── Запасной адрес ──────────────────────────────────────────────────────────

class TestFallback:
    def test_network_failure_goes_to_the_next_address(self, calls):
        write_settings({'deribit': {'fallbacks': ['https://d2.example']}})
        seen, answers = calls
        answers['https://d2.example'] = {'ok': 1}
        assert sources.get('deribit', '/p') == {'ok': 1}
        assert [u for u, _, _ in seen] == ['https://www.deribit.com/p', 'https://d2.example/p']

    def test_not_found_is_not_retried_elsewhere(self, calls):
        write_settings({'deribit': {'fallbacks': ['https://d2.example']}})
        seen, answers = calls
        answers['https://www.deribit.com'] = urllib.error.HTTPError('u', 404, 'нет', {}, None)
        with pytest.raises(urllib.error.HTTPError):
            sources.get('deribit', '/p')
        assert len(seen) == 1

    def test_server_error_is_retried_elsewhere(self, calls):
        write_settings({'deribit': {'fallbacks': ['https://d2.example']}})
        seen, answers = calls
        answers['https://www.deribit.com'] = urllib.error.HTTPError('u', 503, 'занят', {}, None)
        answers['https://d2.example'] = [1]
        assert sources.get('deribit', '/p') == [1]


# ── Здоровье ────────────────────────────────────────────────────────────────

class TestHealth:
    def row(self, code):
        return next(r for r in sources.snapshot()['sources'] if r['code'] == code)

    def test_no_requests_yet(self):
        assert self.row('coinbase')['status'] == 'нет запросов'

    def test_success_then_failure(self, calls):
        seen, answers = calls
        answers['https://api.exchange.coinbase.com'] = [1]
        sources.get('coinbase', '/a')
        assert self.row('coinbase')['status'] == 'отвечает'
        answers['https://api.exchange.coinbase.com'] = urllib.error.URLError('обрыв')
        with pytest.raises(urllib.error.URLError):
            sources.get('coinbase', '/a')
        row = self.row('coinbase')
        assert row['status'] == 'сбой' and 'обрыв' in row['error'] and row['ok'] == 1 and row['fail'] == 1

    def test_proxy_password_never_reaches_the_panel(self):
        write_settings({'bybit': {'proxy': 'http://user:secret@10.0.0.5:3128'}})
        row = self.row('bybit')
        assert 'secret' not in json.dumps(sources.snapshot())
        assert row['proxy'] == 'http://***@10.0.0.5:3128'

    def test_every_source_is_described(self):
        for row in sources.snapshot()['sources']:
            assert row['title'] and row['url'] and row['provides'] and row['readers'], row['code']


# ── Рыночный клиент ccxt ────────────────────────────────────────────────────

class FakeClient:
    def __init__(self):
        self.urls = {'api': {'public': 'https://api.{hostname}', 'v5': {'x': 'https://api.{hostname}/v5'}}}
        self.fail = False

    def fetch(self, url, method='GET', headers=None, body=None):
        if self.fail:
            raise ConnectionError('обрыв')
        return {'url': url}


class TestCcxtClient:
    def test_defaults_leave_addresses_alone_and_count_health(self):
        client = sources.instrument_ccxt(FakeClient(), 'bybit')
        assert client.urls['api'] == {'public': 'https://api.{hostname}',
                                      'v5': {'x': 'https://api.{hostname}/v5'}}
        assert not hasattr(client, 'httpsProxy')
        client.fetch('https://api.bybit.com/v5/market/time')
        client.fail = True
        with pytest.raises(ConnectionError):
            client.fetch('https://api.bybit.com/v5/market/time')
        row = next(r for r in sources.snapshot()['sources'] if r['code'] == 'bybit')
        assert row['ok'] == 1 and row['fail'] == 1 and row['status'] == 'сбой'

    def test_operator_address_and_proxy(self):
        write_settings({'bybit': {'url': 'https://api.bytick.com', 'proxy': 'http://10.0.0.5:3128'}})
        client = sources.instrument_ccxt(FakeClient(), 'bybit')
        assert client.urls['api'] == {'public': 'https://api.bytick.com',
                                      'v5': {'x': 'https://api.bytick.com/v5'}}
        assert client.httpsProxy == 'http://10.0.0.5:3128'

    def test_real_market_client_goes_through_the_registry(self, monkeypatch):
        from data import exchange
        monkeypatch.setattr(exchange, '_market_client', None)
        client = exchange.make_market_client('bybit')
        try:
            assert 'fetch' in vars(client), 'запросы клиента идут мимо реестра'
            assert client.urls['api']['public'] == 'https://api.{hostname}'
        finally:
            monkeypatch.setattr(exchange, '_market_client', None)
