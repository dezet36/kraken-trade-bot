"""
Бой обязан наблюдать за собой так же, как бумага.

ОТКУДА ЭТО. Разбор проекта 30 августа 2026 показал разрыв: бумажный журнал вёл
56 колонок, боевой — 38, а журнала отказов и наблюдения после выхода на боевом
пути не было вовсе. Разрыв возник не по замыслу — улучшения делались там, куда
смотрели, и второй путь тихо отставал.

Дефект тут не в одной пропущенной колонке, а в СПОСОБЕ промахнуться, поэтому и
проверки написаны про способ: у обоих путей должны быть одни и те же средства
наблюдения и одни и те же имена для одного и того же.
"""

import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import follow_up
import refused


class TestBothPathsAreNamedTheSame:
    """
    Одно и то же должно называться одинаково в обоих журналах.

    Разойдясь, имена заставляют разбор писать две ветки под одно понятие — и
    рано или поздно одну из них забывают обновить.
    """

    def test_the_costs_reach_the_live_journal(self):
        import paper_broker
        import trade_journal

        measured = {'gross_pnl_usd', 'fees_usd', 'funding_usd', 'pnl_r',
                    'cost_share_pct', 'mfe_r', 'mae_r', 'mfe_min', 'mae_min',
                    'atr_pct', 'hour_utc'}
        missing = measured - set(trade_journal.COLUMNS)
        assert not missing, f'боевой журнал снова отстал: {sorted(missing)}'
        # И те же имена на бумаге — иначе сравнить два пути будет нечем.
        assert not (measured - set(paper_broker.COLUMNS))

    def test_the_live_journal_says_where_the_number_came_from(self):
        """
        Замеренное и посчитанное нельзя держать в одной колонке без пометки.

        Комиссии берутся у биржи, а при её отказе оцениваются по тарифу.
        Смешав их молча, через месяц не отличишь факт от модели.
        """
        import trade_journal
        assert 'fees_source' in trade_journal.COLUMNS


class TestRefusalsAreKeptApartByMode:
    """
    Файл отказов один на бумагу и бой. Без разделения выборки смешиваются.

    Так уже испортили журнал сервера две одновременно работавшие копии бота:
    числа выглядели рыночными, а означали не то.
    """

    def test_the_mode_is_written(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(refused, 'CSV_PATH', str(tmp_path / 'refused.csv'))
        signal = {'trading_pair': 'BTCUSDT', 'setup': {'type': 'LONG'},
                  'params': {'entry': 100, 'stop_loss': 98,
                             'take_profit_1': 104, 'rr': 2}}

        refused.record('FIBO', signal, 'предел издержек', 'дорого', 7.5)

        with open(refused.CSV_PATH, encoding='utf-8-sig', newline='') as fh:
            row = next(iter(csv.DictReader(fh)))
        assert row['mode'] == config.TRADING_MODE
        assert row['gate'] == 'предел издержек'
        assert row['cost_share_pct'] == '7.5'

    def test_setup_stays_identifiable(self, tmp_path, monkeypatch):
        """
        Цена, стоп и цель обязаны сохраниться.

        Без них отказ нельзя потом прогнать и спросить, что было бы, — а ради
        этого журнал и заводился. И группируются отказы по (пара, вход, стоп):
        один сетап предлагается каждый цикл и пишется новой строкой.
        """
        monkeypatch.setattr(refused, 'CSV_PATH', str(tmp_path / 'refused.csv'))
        signal = {'trading_pair': 'ETHUSDT', 'setup': {'type': 'SHORT'},
                  'params': {'entry': 50.5, 'stop_loss': 52.0,
                             'take_profit_1': 47.0, 'rr': 2.3}}

        for _ in range(3):                       # три цикла подряд — один сетап
            refused.record('SMC', signal, 'кулдаун')

        with open(refused.CSV_PATH, encoding='utf-8-sig', newline='') as fh:
            rows = list(csv.DictReader(fh))

        assert len(rows) == 3
        setups = {(r['pair'], r['entry'], r['stop_loss']) for r in rows}
        assert len(setups) == 1                  # три строки, но сетап один
        assert rows[0]['tp1'] == '47.0'


class TestTheLiveWatchSurvivesARestart:
    """
    Наблюдение живёт 12 часов, а бот за это время перезапускается.

    Держи мы его в памяти, до записи доживали бы только те наблюдения, которым
    повезло с простоем, — и выборка сместилась бы в сторону спокойных
    периодов. Это та же ошибка, что и потеря сделок при простое: числа
    остаются правдоподобными, а отбор уже неслучаен.
    """

    @pytest.fixture(autouse=True)
    def _own_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(follow_up, 'STATE_PATH', str(tmp_path / 'state.json'))
        monkeypatch.setattr(follow_up, 'CSV_PATH', str(tmp_path / 'follow_up.csv'))

    def live_position(self):
        return {
            'entry_price': 100.0,
            'signal': {'setup': {'type': 'LONG'}},
            'params': {'stop_loss': 98.0, 'tp_targets': [104.0],
                       'tp_fractions': [1.0]},
        }

    def test_a_closed_trade_leaves_a_watch_on_disk(self):
        follow_up.watch_live(self.live_position(), 'BTCUSDT', 101.0, 'TP1', 7, 'FIBO')

        saved = json.load(open(follow_up.STATE_PATH, encoding='utf-8'))
        assert len(saved) == 1
        assert saved[0]['pair'] == 'BTCUSDT'
        assert saved[0]['trade_id'] == 7
        assert saved[0]['stop_loss'] == 98.0

    def test_price_after_the_exit_is_measured_from_the_entry(self):
        """
        R считается от ВХОДА, а не от выхода.

        Вопрос в том, дошла бы сделка до цели, а не в том, куда вообще ушла
        цена. Отсчёт от выхода отвечал бы на другой вопрос — и незаметно:
        числа выглядели бы так же правдоподобно.
        """
        follow_up.watch_live(self.live_position(), 'BTCUSDT', 101.0, 'BE', 8, 'FIBO')
        state = json.load(open(follow_up.STATE_PATH, encoding='utf-8'))
        start = state[0]['closed_ts']

        hour = 3_600_000
        candles = [[start + hour, 101, 106.0, 100.0, 105.0],
                   [start + 13 * hour, 105, 105.0, 103.0, 104.0]]
        done = follow_up.advance_live(fetch_candles=lambda pair, since: candles)

        assert done == 1
        with open(follow_up.CSV_PATH, encoding='utf-8-sig', newline='') as fh:
            row = next(iter(csv.DictReader(fh)))
        # Максимум 106 при входе 100 и стопе 98 -> +3.0R от входа.
        assert float(row['best_after_r']) == pytest.approx(3.0)
        assert row['hit_tp1_after'] == '1'        # 104 задета уже после выхода
        assert row['hit_sl_after'] == '0'

    def test_a_finished_watch_leaves_the_state(self):
        """Досмотренное уходит в файл и больше не занимает место и запросы."""
        follow_up.watch_live(self.live_position(), 'BTCUSDT', 101.0, 'TP1', 9, 'FIBO')
        start = json.load(open(follow_up.STATE_PATH, encoding='utf-8'))[0]['closed_ts']

        follow_up.advance_live(
            fetch_candles=lambda p, s: [[start + 13 * 3_600_000,
                                         101, 102.0, 100.0, 101.0]])

        assert json.load(open(follow_up.STATE_PATH, encoding='utf-8')) == []

    def test_an_unfinished_watch_stays(self):
        """Недосмотренное остаётся: записать его сейчас — записать полуправду."""
        follow_up.watch_live(self.live_position(), 'BTCUSDT', 101.0, 'TP1', 10, 'FIBO')
        start = json.load(open(follow_up.STATE_PATH, encoding='utf-8'))[0]['closed_ts']

        done = follow_up.advance_live(
            fetch_candles=lambda p, s: [[start + 2 * 3_600_000,
                                         101, 102.0, 100.0, 101.0]])

        assert done == 0
        assert len(json.load(open(follow_up.STATE_PATH, encoding='utf-8'))) == 1
        assert not os.path.exists(follow_up.CSV_PATH)

    def test_an_exchange_failure_does_not_lose_the_watch(self):
        """
        Биржа не ответила — наблюдение обязано дождаться следующего цикла.

        Потерять его тише всего именно здесь: сбоя не видно, а в выборке
        окажутся только те сделки, чьи запросы прошли.
        """
        follow_up.watch_live(self.live_position(), 'BTCUSDT', 101.0, 'TP1', 11, 'FIBO')

        def boom(pair, since):
            raise RuntimeError('биржа недоступна')

        assert follow_up.advance_live(fetch_candles=boom) == 0
        assert len(json.load(open(follow_up.STATE_PATH, encoding='utf-8'))) == 1

    def test_nothing_to_watch_costs_no_requests(self):
        """Пустое состояние не должно ходить в сеть."""
        def never(pair, since):
            raise AssertionError('запрос при пустом состоянии')

        assert follow_up.advance_live(fetch_candles=never) == 0
