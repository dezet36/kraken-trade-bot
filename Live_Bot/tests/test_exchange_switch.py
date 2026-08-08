"""
Подключение второй биржи: символы, выбор и различия в возможностях.

ГЛАВНОЕ, ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, — ПРИВЕДЕНИЕ СИМВОЛОВ. Пул задан биржевыми
символами Bybit ('BTCUSDT'). BingX называет тот же рынок 'BTC-USDT' и на нашу
запись отвечает BadSymbol — проверено живым запросом. Без приведения вторая
биржа не работает вообще: ни свечей, ни ордеров.

Второе — что различия в возможностях учитываются, а не игнорируются. У BingX из
четырёх источников данных о позиционировании есть только фандинг. Сборщик,
который этого не знает, каждый час писал бы в журнал три отказа, и выглядело бы
это как поломка.

Третье — что ключи НЕ ходят через настройки. Дашборд слушает без пароля, и
секрет в файле настроек означал бы, что его видно всем, кто открыл страницу.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeMarket(dict):
    pass


class FakeExchange:
    """Биржа со своей записью символов и своим набором возможностей."""

    def __init__(self, ident, ids, caps=()):
        self.id = ident
        # Рынки пусты до load_markets — как у настоящего клиента ccxt. С
        # заранее заполненным словарём проверка кэширования была бы пустой:
        # загрузка не вызывалась бы ни разу и «ноль вызовов» ничего не значило.
        self.markets = {}
        self._ids = list(ids)
        self.has = {name: True for name in caps}
        self.loaded = 0

    def load_markets(self):
        self.loaded += 1
        for native in self._ids:
            base = native.replace('-', '').replace('USDT', '')
            self.markets[f'{base}/USDT:USDT'] = {'id': native,
                                                 'symbol': f'{base}/USDT:USDT'}
        return self.markets


BYBIT_IDS = ['BTCUSDT', 'ETHUSDT', 'SHIB1000USDT']
BINGX_IDS = ['BTC-USDT', 'ETH-USDT']          # SHIB1000 у него нет


@pytest.fixture()
def ex(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('config', 'settings_store', 'exchange'):
        sys.modules.pop(module, None)
    import exchange
    exchange._symbol_cache.clear()
    return exchange


class TestSymbolMapping:
    def test_same_pair_resolves_per_exchange(self, ex):
        bybit = FakeExchange('bybit', BYBIT_IDS)
        bingx = FakeExchange('bingx', BINGX_IDS)
        assert ex.market_symbol('BTCUSDT', bybit) == 'BTC/USDT:USDT'
        assert ex.market_symbol('BTCUSDT', bingx) == 'BTC/USDT:USDT'

    def test_missing_market_returns_none(self, ex):
        """
        У BingX нет одной из наших пар. Молчаливая подстановка отправляла бы в
        запросы символ, которого там не существует, — по разу на каждый цикл.
        """
        bingx = FakeExchange('bingx', BINGX_IDS)
        assert ex.market_symbol('SHIB1000USDT', bingx) is None

    def test_available_pairs_drops_the_missing(self, ex):
        bingx = FakeExchange('bingx', BINGX_IDS)
        pairs = ['BTCUSDT', 'ETHUSDT', 'SHIB1000USDT']
        assert ex.available_pairs(pairs, bingx) == ['BTCUSDT', 'ETHUSDT']

    def test_mapping_is_cached_per_exchange(self, ex):
        """Рынки не должны перечитываться на каждый запрос свечей."""
        bybit = FakeExchange('bybit', BYBIT_IDS)
        for _ in range(5):
            ex.market_symbol('BTCUSDT', bybit)
        assert bybit.loaded == 1

    def test_cache_does_not_mix_exchanges(self, ex):
        """
        Ключ кэша обязан включать биржу: иначе вторая получила бы запись
        первой, и ошибка проявилась бы только в бою.
        """
        bybit = FakeExchange('bybit', BYBIT_IDS)
        bingx = FakeExchange('bingx', BINGX_IDS)
        ex.market_symbol('SHIB1000USDT', bybit)
        assert ex.market_symbol('SHIB1000USDT', bingx) is None


class TestCapabilities:
    def test_unknown_capability_fails_open(self, ex):
        """
        «Не удалось спросить» — это сбой, а не отсутствие данных. Осторожный
        ответ «нет» тихо отключил бы весь сбор на упавшей бирже, и в журнале
        не осталось бы ни строчки.
        """
        class Dead:
            def __getattr__(self, name):
                raise RuntimeError('биржа недоступна')

        assert ex.supports(Dead(), 'fetchOpenInterestHistory') is True

    def test_supports_reports_the_difference(self, ex):
        rich = FakeExchange('bybit', BYBIT_IDS, caps=ex.CAPABILITIES)
        poor = FakeExchange('bingx', BINGX_IDS,
                            caps=('fetchFundingRateHistory',))
        assert ex.supports(rich, 'fetchOpenInterestHistory') is True
        assert ex.supports(poor, 'fetchOpenInterestHistory') is False
        assert ex.supports(poor, 'fetchFundingRateHistory') is True

    def test_collector_skips_unsupported_sources(self, ex, monkeypatch,
                                                 tmp_path):
        """
        Источник, которого у биржи нет, — это её свойство, а не сбой. Сборщик
        обязан молчать: иначе на BingX он писал бы три отказа каждый час.
        """
        sys.modules.pop('positioning', None)
        import positioning

        lines = []
        monkeypatch.setattr(positioning, 'log', lines.append)
        poor = FakeExchange('bingx', BINGX_IDS,
                            caps=('fetchFundingRateHistory',))
        poor.fetch_funding_rate_history = lambda *a, **k: []

        written = positioning.collect(poor, pairs=['BTCUSDT'])
        assert written['open_interest'] == 0
        assert written['premium'] == 0
        assert lines == []              # ни одной жалобы


class TestSelection:
    def test_choice_requires_configured_keys(self, ex, monkeypatch):
        """
        Выбор биржи без ключей не должен применяться: бот упал бы при старте
        на решении, принятом когда-то мышкой.
        """
        import settings_store
        settings_store.save({settings_store.EXCHANGE: {'name': 'bingx'}})
        monkeypatch.setattr(ex, 'configured_exchanges',
                            lambda: {'bybit': True, 'bingx': False})
        assert ex.active_exchange_name() == 'bybit'

    def test_choice_applies_when_configured(self, ex, monkeypatch):
        import settings_store
        settings_store.save({settings_store.EXCHANGE: {'name': 'bingx'}})
        monkeypatch.setattr(ex, 'configured_exchanges',
                            lambda: {'bybit': True, 'bingx': True})
        assert ex.active_exchange_name() == 'bingx'

    def test_unknown_exchange_ignored(self, ex):
        import settings_store
        saved = settings_store.save({settings_store.EXCHANGE:
                                     {'name': 'кракен-которого-нет'}})
        assert saved[settings_store.EXCHANGE]['name'] in ex.SUPPORTED_EXCHANGES


class TestKeysNeverPassThroughSettings:
    def test_settings_file_holds_no_secrets(self, ex, tmp_path):
        """
        Дашборд слушает без пароля. Ключ, попавший в файл настроек, стал бы
        виден всем, кто открыл страницу, — и это не гипотетическая придирка,
        а прямое следствие того, что файл отдаётся в /api/settings.
        """
        import settings_store
        settings_store.save({settings_store.EXCHANGE:
                             {'name': 'bingx', 'api_key': 'секрет',
                              'secret': 'тоже секрет'}})
        stored = settings_store.load(force=True)[settings_store.EXCHANGE]
        assert set(stored) == {'name'}
        assert 'секрет' not in str(stored)
