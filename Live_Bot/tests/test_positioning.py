"""
Сборщик данных о позиционировании.

ГЛАВНОЕ, ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, — ЧТО СБОР НЕ МОЖЕТ ПОМЕШАТЬ ТОРГОВЛЕ. Модуль
живёт внутри торгового цикла, и любая его ошибка — отказ биржи, битый файл,
неожиданный ответ — не должна поднимать исключение наверх. Сбор данных впрок
не стоит ни одной пропущенной сделки.

Второе — отсутствие повторов. История отдаётся с большим перекрытием (200
записей при часовом шаге — это восемь дней), и каждый час почти всё, что
приходит, уже лежит в файле. Без отсева хранилище за месяц раздулось бы в
двести раз и стало бы непригодным для замера.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('config', 'positioning'):
        sys.modules.pop(module, None)
    import positioning
    positioning._seen = None
    positioning._last_run = 0.0
    return positioning


class FakeClient:
    """
    Биржа, отдающая предсказуемые записи. Может ломаться по требованию.

    Объявляет ВОЗМОЖНОСТИ и рынки, как настоящий клиент ccxt: сборщик теперь
    спрашивает `has` перед обращением к источнику (у BingX три источника из
    четырёх отсутствуют), а символы приводит к записи конкретной биржи.
    Подделка без этих полей выглядела бы биржей, не умеющей ничего.
    """

    def __init__(self, rows=3, fail=(), caps=None):
        self.rows = rows
        self.fail = set(fail)
        self.calls = 0
        self.id = 'fake'
        self.markets = {'BTC/USDT:USDT': {'id': 'BTCUSDT'},
                        'ETH/USDT:USDT': {'id': 'ETHUSDT'},
                        'SOL/USDT:USDT': {'id': 'SOLUSDT'}}
        self.has = {name: True for name in (caps or (
            'fetchOpenInterestHistory', 'fetchLongShortRatioHistory',
            'fetchFundingRateHistory', 'fetchPremiumIndexOHLCV'))}

    def load_markets(self):
        return self.markets

    def _series(self, kind):
        self.calls += 1
        if kind in self.fail:
            raise RuntimeError(f'{kind} недоступен')
        return self.rows

    def fetch_open_interest_history(self, pair, tf, limit=None):
        n = self._series('open_interest')
        return [{'timestamp': 1_000 + i * 3_600_000,
                 'openInterestAmount': 100 + i,
                 'openInterestValue': 1000 + i} for i in range(n)]

    def fetch_long_short_ratio_history(self, pair, tf, limit=None):
        n = self._series('long_short')
        return [{'timestamp': 1_000 + i * 3_600_000,
                 'longShortRatio': 1.5} for i in range(n)]

    def fetch_funding_rate_history(self, pair, limit=None):
        n = self._series('funding')
        return [{'timestamp': 1_000 + i * 28_800_000,
                 'fundingRate': 0.0001} for i in range(n)]

    def fetch_premium_index_ohlcv(self, pair, tf, limit=None):
        n = self._series('premium')
        return [[1_000 + i * 3_600_000, 1, 1, 1, 0.02, 0] for i in range(n)]


class TestCollection:
    def test_writes_every_source(self, store):
        written = store.collect(FakeClient(), pairs=['BTCUSDT'])
        assert set(written) == set(store.SOURCES)
        assert all(count == 3 for count in written.values())
        for source in store.SOURCES:
            assert os.path.exists(store.path_for(source))

    def test_rows_carry_pair_and_time(self, store):
        store.collect(FakeClient(rows=2), pairs=['BTCUSDT', 'ETHUSDT'])
        with open(store.path_for('open_interest'), encoding='utf-8') as fh:
            rows = [json.loads(line) for line in fh]
        assert len(rows) == 4
        assert {r['pair'] for r in rows} == {'BTCUSDT', 'ETHUSDT'}
        assert all(isinstance(r['ts'], int) for r in rows)
        assert all(r['value'] is not None for r in rows)


class TestNoDuplicates:
    def test_second_pass_adds_nothing(self, store):
        """
        История приходит с перекрытием в восемь дней: без отсева хранилище
        росло бы в двести раз быстрее нужного.
        """
        client = FakeClient(rows=5)
        first = store.collect(client, pairs=['BTCUSDT'])
        second = store.collect(client, pairs=['BTCUSDT'])
        assert all(count == 5 for count in first.values())
        assert all(count == 0 for count in second.values())

    def test_only_new_records_appended(self, store):
        store.collect(FakeClient(rows=3), pairs=['BTCUSDT'])
        added = store.collect(FakeClient(rows=5), pairs=['BTCUSDT'])
        assert added['open_interest'] == 2

    def test_dedupe_survives_restart(self, store):
        """Ключи перечитываются с диска — иначе перезапуск удваивал бы файл."""
        store.collect(FakeClient(rows=4), pairs=['BTCUSDT'])
        store._seen = None                     # как после перезапуска
        again = store.collect(FakeClient(rows=4), pairs=['BTCUSDT'])
        assert all(count == 0 for count in again.values())


class TestFailuresAreContained:
    def test_broken_source_does_not_stop_others(self, store):
        written = store.collect(FakeClient(rows=3, fail={'open_interest'}),
                                pairs=['BTCUSDT'])
        assert written['open_interest'] == 0
        assert written['long_short'] == 3

    def test_exchange_failure_never_raises(self, store):
        class Dead:
            def __getattr__(self, name):
                raise RuntimeError('биржа недоступна')

        # collect_if_due вызывается из торгового цикла: исключение отсюда
        # остановило бы ведение позиций. Полностью мёртвая биржа — это ноль
        # записей, а не отказ: каждая пара гасится по отдельности.
        written = store.collect_if_due(Dead(), pairs=['BTCUSDT'])
        assert written is not None
        assert all(count == 0 for count in written.values())

    def test_dead_exchange_logs_once_per_source(self, store, monkeypatch):
        """
        Построчный отчёт об отказах давал 84 записи в журнал при недоступной
        бирже — каждый час. Настоящая причина в них тонула.
        """
        lines = []
        monkeypatch.setattr(store, 'log', lines.append)

        class Dead:
            def __getattr__(self, name):
                raise RuntimeError('биржа недоступна')

        store.collect(Dead(), pairs=['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
        assert len(lines) == len(store.SOURCES)
        assert all('3 парам из 3' in line for line in lines)

    def test_broken_file_does_not_stop_collection(self, store):
        os.makedirs(store.store_dir(), exist_ok=True)
        with open(store.path_for('funding'), 'w', encoding='utf-8') as fh:
            fh.write('{ это не json\n')
        store._seen = None
        written = store.collect(FakeClient(rows=2), pairs=['BTCUSDT'])
        assert written['funding'] == 2


class TestSchedule:
    def test_runs_once_per_interval(self, store, monkeypatch):
        client = FakeClient(rows=2)
        assert store.collect_if_due(client, pairs=['BTCUSDT']) is not None
        assert store.collect_if_due(client, pairs=['BTCUSDT']) is None
        store._last_run -= store.INTERVAL_SEC + 1
        assert store.collect_if_due(client, pairs=['BTCUSDT']) is not None


class TestSummary:
    def test_reports_span_and_pairs(self, store):
        store.collect(FakeClient(rows=25), pairs=['BTCUSDT', 'ETHUSDT'])
        info = store.summary()['open_interest']
        assert info['rows'] == 50
        assert info['pairs'] == 2
        assert info['days'] == pytest.approx(24 / 24, abs=0.01)

    def test_empty_store_reports_zero(self, store):
        assert store.summary()['funding']['rows'] == 0
