"""
Наблюдение закрывается на последней отсечке HORIZONS — с 23.09.2026 это
168 часов, поэтому ряды баров здесь оканчиваются свечой на 168-м часе.

Исходы вердиктов: куда пошла цена после каждого разбора.

Разбор читается убедительно при любом решении. Различить верное от
ошибочного можно только по ходу цены — этот файл и есть тот замер.
"""

import os
import json
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
            (12, 106.5, 103, 106), (24, 107, 105, 106.5), (48, 107, 105, 106.5), (168, 107, 105, 106.5)])
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
        done = run_bars('ETHUSDT', start, [(2, 102.5, 99, 101), (24, 103, 100, 102), (48, 103, 100, 102), (168, 103, 100, 102)])
        row = llm_outcomes.row(done[0])
        assert row['decision'] == 'skip' and row['gate'] == 'критик отклонил'
        assert row['hit_sl'] == 1 and row['hit_tp1'] == 0
        assert row['worst_r'] == pytest.approx(-(103 - 100) / 2)

    def test_a_code_refusal_is_watched_by_its_plan(self):
        """Отказ кода несёт план в verdict['plan'] — исход отклонённого досматривается."""
        start = 1_700_000_000_000
        llm_outcomes.watch('XRPUSDT', {'ok': False, 'gate': 'мало конфлюенса',
                                       'plan': {'side': 'LONG', 'entry': 100.0, 'stop': 98.0,
                                                'targets': [104.0]}},
                           price=100.2, ts=start)
        done = run_bars('XRPUSDT', start, [(3, 104.5, 99.8, 104), (24, 105, 103, 104),
                                           (48, 105, 103, 104), (168, 105, 103, 104)])
        row = llm_outcomes.row(done[0])
        assert row['decision'] == 'skip' and row['side'] == 'LONG'
        assert row['hit_tp1'] == 1 and row['hit_sl'] == 0 and row['entry_touched'] == 1


class TestASkipIsMeasuredInPercent:

    def test_no_direction_no_r(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('SOLUSDT', {'ok': False, 'gate': 'модель пропустила'},
                           price=100.0, ts=start)
        done = run_bars('SOLUSDT', start, [(4, 103, 98, 102), (24, 104, 97, 99), (48, 104, 97, 99), (168, 104, 97, 99)])
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
        stale = llm_outcomes.expire(start + 9 * 24 * H)
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
                                    (12, 106.5, 103, 106), (24, 107, 105, 106.5), (48, 107, 105, 106.5), (168, 107, 105, 106.5)])
        rec = llm_outcomes.recent('ETHUSDT')
        assert len(rec) == 1 and rec[0]['done'] is True
        assert rec[0]['hit_tp1'] is True and rec[0]['pct'] == pytest.approx(6.5, abs=1e-3)

    def test_nothing_known_gives_nothing(self):
        assert llm_outcomes.recent('XRPUSDT') == []


class TestTheDeeperOutcomeFields:
    """Когда дошла до цели/стопа, коснулась ли входа, ход без нас, условие, реакция уровней."""

    def test_timing_entry_and_missed_move(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('SUIUSDT', {'ok': True, 'gate': '', 'side': 'LONG', 'entry': 100.0, 'stop': 97.0,
                                       'targets': [106.0], 'trigger_when': 'retest', 'trigger_level': 100.0,
                                       'atr_pct': 2.0,
                                       'levels': [{'id': 'L1', 'price': 106.0, 'kind': 'пул'},
                                                  {'id': 'L2', 'price': 100.0, 'kind': 'вход'}]},
                           price=101.0, ts=start)
        # Цена ни разу не дошла до 100: минимум 100.4, ушла к цели за 6 ч.
        done = run_bars('SUIUSDT', start, [(1, 102, 100.4, 101.5), (3, 104, 101, 103.5),
                                           (6, 106.5, 103, 106), (24, 108, 105, 107), (48, 108, 105, 107), (168, 108, 105, 107)])
        row = llm_outcomes.row(done[0])
        assert row['hit_tp1'] == 1 and row['tp_hours'] == pytest.approx(6.0)
        assert row['hit_sl'] == 0 and row['sl_hours'] == ''
        assert row['entry_touched'] == 0 and row['entry_hours'] == ''
        assert row['min_dist_entry_pct'] == pytest.approx(0.4)
        assert row['missed_move_r'] == pytest.approx((108 - 100) / 3, abs=1e-3), 'ход к цели без нас в R'
        assert row['trigger_when'] == 'retest'
        levels = json.loads(row['levels_hit'])
        l1 = next(lv for lv in levels if lv['id'] == 'L1')
        assert l1['touched_h'] == pytest.approx(6.0) and l1['react_atr'] is not None
        l2 = next(lv for lv in levels if lv['id'] == 'L2')
        assert l2['touched_h'] is None, 'вход не коснулись — уровень не тронут'

    def test_entry_touched_then_stopped(self):
        start = 1_700_000_000_000
        llm_outcomes.watch('ETHUSDT', {'ok': True, 'gate': '', 'side': 'LONG', 'entry': 100.0, 'stop': 97.0,
                                       'targets': [106.0], 'trigger_when': 'now'}, price=101.0, ts=start)
        done = run_bars('ETHUSDT', start, [(2, 101, 99.8, 100.2), (5, 100.5, 96.8, 97.2),
                                           (24, 99, 97, 98), (48, 99, 97, 98), (168, 99, 97, 98)])
        row = llm_outcomes.row(done[0])
        assert row['entry_touched'] == 1 and row['entry_hours'] == pytest.approx(2.0)
        assert row['hit_sl'] == 1 and row['sl_hours'] == pytest.approx(5.0)
        assert row['missed_move_r'] == '', 'вход был — «без нас» не считается'
        assert row['cond_hours'] == '' and row['trigger_when'] == 'now'

    def test_the_condition_is_evaluated_on_hourly_bars(self, monkeypatch):
        import strategy_llm
        start = 1_700_000_000_000 - (1_700_000_000_000 % H)
        seen = []
        monkeypatch.setattr(strategy_llm, 'condition_met',
                            lambda when, level, side, bars, median: seen.append(len(bars)) or (len(bars) >= 2))
        llm_outcomes.watch('XRPUSDT', {'ok': True, 'gate': '', 'side': 'LONG', 'entry': 100.0, 'stop': 97.0,
                                       'targets': [106.0], 'trigger_when': 'close_above', 'trigger_level': 101.0},
                           price=100.0, ts=start)
        bars = [(h / 12, 100.5, 99.5, 100.2) for h in range(1, 12 * 4)] + [(24, 101, 99, 100), (48, 101, 99, 100), (168, 101, 99, 100)]
        done = run_bars('XRPUSDT', start, bars)
        row = llm_outcomes.row(done[0])
        assert seen and max(seen) >= 2, 'условие проверялось по часовым свечам'
        assert row['cond_hours'] != '' and float(row['cond_hours']) >= 2
