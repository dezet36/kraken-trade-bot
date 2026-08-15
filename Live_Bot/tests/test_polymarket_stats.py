"""
Статистика, по которой можно улучшать маркет-мейкера.

ПАНЕЛЬ ОТВЕЧАЕТ НА «ЧТО СЕЙЧАС», А УЛУЧШАТЬ МОЖНО ТОЛЬКО ПО «ЧТО ВЫШЛО». Это
разные наборы чисел и разные источники: первый живёт в памяти работающего
потока, второй — в журналах на диске, которые переживают перезапуск.

Каждое число здесь меняет конкретную настройку:

    какие рынки платят        отбор, MM_MIN_USD_PER_HOUR, MM_MAX_WAIT_HOURS
    сколько спреда взяли      глубину шага внутрь спреда
    куда шла цена после нас   сам вывод, стоит ли этим заниматься
    обещание против дела      поправку на оптимизм модели
    доля исполнения           вставать внутрь спреда или на лучшую цену
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import engine, executor, mm, stats  # noqa: E402


def _write(path, rows):
    with open(path, 'w', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')


@pytest.fixture
def journals(monkeypatch):
    folder = tempfile.mkdtemp(prefix='stats-')
    paths = {name: os.path.join(folder, name + '.jsonl')
             for name in ('fills', 'timing', 'drift', 'orders')}
    paths['plan'] = os.path.join(folder, 'plan.json')
    monkeypatch.setattr(engine, 'FILLS', paths['fills'])
    monkeypatch.setattr(engine, 'TIMING', paths['timing'])
    monkeypatch.setattr(engine, 'DRIFT', paths['drift'])
    monkeypatch.setattr(executor, 'ORDERS_LOG', paths['orders'])
    monkeypatch.setattr(mm, 'PLAN_FILE', paths['plan'])
    return paths


class TestOverview:

    def test_empty_journals_say_nothing_happened(self, journals):
        got = stats.overview()
        assert got['fills'] == 0 and got['rounds'] == 0
        assert got['fill_rate'] is None, 'ни одной заявки — доли не существует'

    def test_a_closed_circle_is_counted_with_its_gain(self, journals):
        _write(journals['fills'], [
            {'at': '1', 'source': 'exchange', 'token': 'T', 'side': 'bid',
             'price': 0.20, 'size': 5, 'question': 'рынок'},
            {'at': '2', 'source': 'exchange', 'token': 'T', 'side': 'ask',
             'price': 0.24, 'size': 5, 'question': 'рынок'},
        ])
        got = stats.overview()
        assert got['fills'] == 2
        assert got['rounds'] == 1
        assert got['gain_usd'] == pytest.approx(0.20)
        assert got['open_fills'] == 0

    def test_an_unclosed_fill_is_not_a_circle(self, journals):
        """
        Купить может каждый. Пока позиция не закрыта, заработка нет — и
        показывать его нельзя.
        """
        _write(journals['fills'], [
            {'at': '1', 'source': 'exchange', 'token': 'T', 'side': 'bid',
             'price': 0.20, 'size': 5},
        ])
        got = stats.overview()
        assert got['rounds'] == 0 and got['open_fills'] == 1
        assert got['gain_usd'] == 0

    def test_paper_fills_do_not_pollute_the_result(self, journals):
        """
        Бумажные исполнения — мнение модели, а не деньги. В отчёте о том, что
        вышло, им места нет.
        """
        _write(journals['fills'], [
            {'at': '1', 'token': 'T', 'side': 'bid', 'price': 0.2, 'size': 5},
            {'at': '2', 'source': 'exchange', 'token': 'T', 'side': 'bid',
             'price': 0.2, 'size': 5},
        ])
        assert stats.overview()['fills'] == 1

    def test_fill_rate_counts_only_placed_orders(self, journals):
        _write(journals['orders'], [
            {'action': 'PLACED'}, {'action': 'PLACED'},
            {'action': 'PLACED'}, {'action': 'PLACED'},
            {'action': 'REFUSE'}, {'action': 'ERROR'},
        ])
        _write(journals['fills'], [
            {'source': 'exchange', 'token': 'T', 'side': 'bid',
             'price': 0.2, 'size': 5},
        ])
        got = stats.overview()
        assert got['orders_placed'] == 4
        assert got['orders_refused'] == 2
        assert got['fill_rate'] == pytest.approx(0.25)

    def test_model_optimism_is_the_median_not_the_average(self, journals):
        """Один выброс не должен решать судьбу поправки."""
        _write(journals['timing'], [{'ratio': 1.0}, {'ratio': 2.0},
                                    {'ratio': 3.0}, {'ratio': 99.0}])
        assert stats.overview()['timing_factor'] == 3.0

    def test_broken_line_does_not_break_the_report(self, journals):
        with open(journals['fills'], 'w', encoding='utf-8') as fh:
            fh.write('{это не json}\n')
            fh.write(json.dumps({'source': 'exchange', 'token': 'T',
                                 'side': 'bid', 'price': 0.2, 'size': 5}) + '\n')
        assert stats.overview()['fills'] == 1


class TestByMarket:

    def test_promised_stands_next_to_what_happened(self, journals):
        """
        ГЛАВНАЯ ТАБЛИЦА ДЛЯ УЛУЧШЕНИЯ ОТБОРА. Расчётный доход — предположение;
        рядом с ним обязано стоять то, что вышло.
        """
        with open(journals['plan'], 'w', encoding='utf-8') as fh:
            json.dump({'markets': [{'token_id': 'T', 'question': 'рынок',
                                    'usd_per_hour': 0.42, 'wait_hours': 0.5}]}, fh)
        _write(journals['fills'], [
            {'source': 'exchange', 'token': 'T', 'side': 'bid',
             'price': 0.20, 'size': 5, 'question': 'рынок'},
            {'source': 'exchange', 'token': 'T', 'side': 'ask',
             'price': 0.24, 'size': 5, 'question': 'рынок'},
        ])
        rows = stats.by_market()
        assert len(rows) == 1
        row = rows[0]
        assert row['rounds'] == 1
        assert row['gain_usd'] == pytest.approx(0.20)
        assert row['promised_per_hour'] == 0.42
        assert row['promised_wait_min'] == 30

    def test_a_market_that_only_buys_is_visible_as_such(self, journals):
        """Исполнения есть, кругов нет — рынок занимает деньги впустую."""
        _write(journals['fills'], [
            {'source': 'exchange', 'token': 'МЁРТВЫЙ', 'side': 'bid',
             'price': 0.5, 'size': 5, 'question': 'тихий рынок'},
        ])
        row = stats.by_market()[0]
        assert row['fills'] == 1 and row['rounds'] == 0
        assert row['open_fills'] == 1
        assert row['spent_usd'] == pytest.approx(2.5)

    def test_best_markets_come_first(self, journals):
        _write(journals['fills'], [
            {'source': 'exchange', 'token': 'A', 'side': 'bid', 'price': 0.2, 'size': 5},
            {'source': 'exchange', 'token': 'A', 'side': 'ask', 'price': 0.21, 'size': 5},
            {'source': 'exchange', 'token': 'B', 'side': 'bid', 'price': 0.2, 'size': 5},
            {'source': 'exchange', 'token': 'B', 'side': 'ask', 'price': 0.30, 'size': 5},
        ])
        rows = stats.by_market()
        assert rows[0]['token'] == 'B', 'кто заработал больше, тот и выше'


class TestExport:

    def test_csv_has_a_row_per_market(self, journals):
        _write(journals['fills'], [
            {'source': 'exchange', 'token': 'A', 'side': 'bid',
             'price': 0.2, 'size': 5, 'question': 'первый'},
            {'source': 'exchange', 'token': 'B', 'side': 'bid',
             'price': 0.3, 'size': 5, 'question': 'второй'},
        ])
        text = stats.to_csv()
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) == 3, 'заголовок и две строки'
        assert 'первый' in text and 'второй' in text

    def test_report_never_raises_on_missing_files(self, journals):
        assert 'overview' in stats.report()
