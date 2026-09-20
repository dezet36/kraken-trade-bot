"""
Исходы вердиктов: куда пошла цена после каждого разбора.

Разбор читается убедительно при любом решении. Различить верное от
ошибочного можно только по ходу цены — этот файл и есть тот замер.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_outcomes

H = 3_600_000


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_outcomes, 'STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setattr(llm_outcomes, 'CSV_PATH', str(tmp_path / 'out.csv'))
    llm_outcomes._watches = None
    yield
    llm_outcomes._watches = None


def run_bars(pair, start, bars):
    """bars: список (часов от начала, high, low, close)."""
    done = []
    for hours, high, low, close in bars:
        done += llm_outcomes.advance(pair, start + int(hours * H), high, low, close)
    return done


class TestAnEntryIsMeasuredInR:

    def test_a_long_that_reaches_the_target(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('BTCUSDT', {'ok': True, 'gate': '', 'side': 'LONG',
                                       'entry': 100.0, 'stop': 97.0, 'targets': [106.0]},
                           price=100.5, ts=start)
        assert llm_outcomes.pairs() == {'BTCUSDT': start}
        done = run_bars('BTCUSDT', start, [
            (0.5, 101, 99.5, 100.8), (1, 102, 100, 101.5), (4, 104, 101, 103.5),
            (12, 106.5, 103, 106), (24, 107, 105, 106.5)])
        assert len(done) == 1
        row = llm_outcomes.row(done[0])
        assert row['decision'] == 'enter' and row['side'] == 'LONG'
        assert row['hit_tp1'] == 1 and row['hit_sl'] == 0
        assert row['best_r'] == pytest.approx((107 - 100) / 3, abs=1e-3)
        assert row['worst_r'] == pytest.approx((99.5 - 100) / 3, abs=1e-3)
        assert row['pct_1h'] == pytest.approx((101.5 / 100.5 - 1) * 100, abs=1e-3)
        assert row['pct_24h'] == pytest.approx((106.5 / 100.5 - 1) * 100, abs=1e-3)
        assert llm_outcomes.pairs() == {}

    def test_a_short_that_is_stopped(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('ETHUSDT', {'ok': False, 'gate': 'критик отклонил',
                                       'side': 'SHORT', 'entry': 100.0, 'stop': 102.0,
                                       'targets': [95.0]}, price=100.0, ts=start)
        done = run_bars('ETHUSDT', start, [(2, 102.5, 99, 101), (24, 103, 100, 102)])
        row = llm_outcomes.row(done[0])
        assert row['decision'] == 'skip' and row['gate'] == 'критик отклонил'
        assert row['hit_sl'] == 1 and row['hit_tp1'] == 0
        assert row['worst_r'] == pytest.approx(-(103 - 100) / 2)


class TestASkipIsMeasuredInPercent:

    def test_no_direction_no_r(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('SOLUSDT', {'ok': False, 'gate': 'модель пропустила'},
                           price=100.0, ts=start)
        done = run_bars('SOLUSDT', start, [(4, 103, 98, 102), (24, 104, 97, 99)])
        row = llm_outcomes.row(done[0])
        assert row['side'] == '' and row['best_r'] == '' and row['hit_tp1'] == ''
        assert row['max_up_pct'] == pytest.approx(4.0)
        assert row['max_down_pct'] == pytest.approx(-3.0)
        assert row['pct_4h'] == pytest.approx(2.0)
        assert row['pct_24h'] == pytest.approx(-1.0)
        # Горизонт 1ч отмечается первой свечой ПОСЛЕ него — здесь это свеча 4ч.
        assert row['pct_1h'] == pytest.approx(2.0)

    def test_a_broken_answer_is_not_watched(self):
        llm_outcomes.watch('SOLUSDT', {'ok': False, 'gate': 'ответ обрезан'},
                           price=100.0, ts=1)
        assert llm_outcomes.pairs() == {}


class TestTheStateSurvivesARestart:

    def test_watches_are_reloaded_from_disk(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('BTCUSDT', {'ok': False, 'gate': 'модель пропустила'},
                           price=100.0, ts=start)
        llm_outcomes._watches = None                 # «перезапуск»
        assert llm_outcomes.pairs() == {'BTCUSDT': start}

    def test_the_same_bar_is_not_counted_twice(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('BTCUSDT', {'ok': False, 'gate': 'модель пропустила'},
                           price=100.0, ts=start)
        llm_outcomes.advance('BTCUSDT', start + H, 110, 90, 100)
        llm_outcomes.advance('BTCUSDT', start + H, 120, 80, 100)   # та же свеча
        w = llm_outcomes._load()[0]
        assert w['high'] == 110 and w['low'] == 90

    def test_abandoned_watches_are_written_by_time(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('DEADUSDT', {'ok': False, 'gate': 'модель пропустила'},
                           price=100.0, ts=start)
        stale = llm_outcomes.expire(start + 5 * 24 * H)
        assert len(stale) == 1 and llm_outcomes.pairs() == {}
        assert os.path.exists(llm_outcomes.CSV_PATH)


class TestRecentOutcomesFeedTheMarkup:
    """Модель видит, куда пошла цена после её прошлого вердикта — и пока
    наблюдение идёт, и когда оно досмотрено."""

    def test_a_running_watch_reports_the_move_so_far(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('ETHUSDT', {'ok': True, 'gate': '', 'side': 'SHORT',
                                       'entry': 100.0, 'stop': 102.0, 'targets': [95.0]},
                           price=100.0, ts=start, at='2026-09-20T10:00:00+00:00')
        run_bars('ETHUSDT', start, [(1, 101.0, 98.5, 99.0), (4, 99.5, 97.0, 97.5)])
        rec = llm_outcomes.recent('ETHUSDT')
        assert len(rec) == 1
        r = rec[0]
        assert r['done'] is False and r['side'] == 'SHORT'
        assert r['pct'] == pytest.approx(-2.5, abs=1e-6)
        assert r['max_down'] == pytest.approx(-3.0, abs=1e-6)
        assert r['hit_tp1'] is False and r['hit_sl'] is False
        assert r['hours'] == pytest.approx(4.0)

    def test_finished_watches_come_from_the_file(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('ETHUSDT', {'ok': True, 'gate': '', 'side': 'LONG',
                                       'entry': 100.0, 'stop': 97.0, 'targets': [106.0]},
                           price=100.0, ts=start, at='2026-09-19T10:00:00+00:00')
        run_bars('ETHUSDT', start, [(1, 101, 99.5, 100.8), (4, 104, 101, 103.5),
                                    (12, 106.5, 103, 106), (24, 107, 105, 106.5)])
        rec = llm_outcomes.recent('ETHUSDT')
        assert len(rec) == 1 and rec[0]['done'] is True
        assert rec[0]['hit_tp1'] is True and rec[0]['pct'] == pytest.approx(6.5, abs=1e-3)

    def test_nothing_known_gives_nothing(self):
        assert llm_outcomes.recent('XRPUSDT') == []
