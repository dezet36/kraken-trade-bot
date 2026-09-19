"""
Событийная очередь: пара со свежим сломом, всплеском ликвидаций, ценой у
уровня или скачком ОИ идёт впереди круга — без новых запросов к бирже.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_urgency
import strategy_llm

NOW = 1_700_000_000_000


def make_df(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        'timestamp': pd.to_datetime(np.arange(len(closes)) * 3_600_000 + NOW - len(closes) * 3_600_000, unit='ms'),
        'open': closes, 'high': closes * 1.002, 'low': closes * 0.998, 'close': closes,
        'volume': np.full(len(closes), 100.0)})


class Ctx:
    def __init__(self, df, last_event_age=None):
        self.frames = {'poi': df}
        n = len(df) - 1
        events = ([{'type': 'BOS', 'direction': 'BULLISH', 'index': n - last_event_age, 'level': 1.0}]
                  if last_event_age is not None else [])
        self.structure = {'events': events, '_event_index': [e['index'] for e in events],
                          'points': []}


@pytest.fixture(autouse=True)
def _quiet_sources(monkeypatch):
    import liquidations
    import positioning
    monkeypatch.setattr(liquidations, 'rows', lambda pair, since=None, upto=None, path=None: [])
    monkeypatch.setattr(positioning, 'series', lambda *a, **k: [])


class TestEvents:

    def test_a_fresh_break_is_urgent(self, monkeypatch):
        import llm_context
        monkeypatch.setattr(llm_context, 'levels', lambda df: ([], 0.0))
        points, why = llm_urgency.score('BTCUSDT', Ctx(make_df(np.linspace(100, 110, 300)), 2), NOW)
        assert points == 3 and 'BOS 2 св. назад' in why[0]

    def test_an_old_break_is_not(self, monkeypatch):
        import llm_context
        monkeypatch.setattr(llm_context, 'levels', lambda df: ([], 0.0))
        points, _ = llm_urgency.score('BTCUSDT', Ctx(make_df(np.linspace(100, 110, 300)), 10), NOW)
        assert points == 0

    def test_price_at_a_level_counts(self, monkeypatch):
        import llm_context
        monkeypatch.setattr(llm_context, 'levels',
                            lambda df: ([{'id': 'L3', 'dist_pct': -0.3}, {'id': 'L1', 'dist_pct': 4.0}], 1.0))
        points, why = llm_urgency.score('BTCUSDT', Ctx(make_df(np.linspace(100, 110, 300))), NOW)
        assert points == 2 and 'цена у L3' in why[0]

    def test_a_liquidation_burst_counts(self, monkeypatch):
        import liquidations
        rows = [{'ts': NOW - h * 3_600_000 - 1000, 'size': 1.0} for h in range(2, 14)]
        rows += [{'ts': NOW - 600_000, 'size': 5.0}]            # последний час: ×5
        monkeypatch.setattr(liquidations, 'rows', lambda pair, since=None, upto=None, path=None: rows)
        points, why = llm_urgency.score('BTCUSDT', None, NOW)
        assert points == 2 and 'ликвидации ×5.0' in why[0]

    def test_an_oi_jump_counts_only_when_fresh(self, monkeypatch):
        import positioning
        monkeypatch.setattr(positioning, 'series', lambda *a, **k: [
            {'ts': NOW - 7_200_000, 'value': 1000.0}, {'ts': NOW - 600_000, 'value': 1015.0}])
        points, why = llm_urgency.score('BTCUSDT', None, NOW)
        assert points == 2 and 'ОИ +1.5%' in why[0]
        monkeypatch.setattr(positioning, 'series', lambda *a, **k: [
            {'ts': NOW - 30 * 3_600_000, 'value': 1000.0}, {'ts': NOW - 10 * 3_600_000, 'value': 1015.0}])
        assert llm_urgency.score('BTCUSDT', None, NOW)[0] == 0


class TestTheQueue:

    def test_an_urgent_pair_goes_first_and_halves_the_reask_window(self, monkeypatch):
        import llm_context
        monkeypatch.setattr(llm_context, 'levels', lambda df: ([], 0.0))
        contexts = {'SOLUSDT': Ctx(make_df(np.linspace(100, 110, 300)), 1)}
        strategy_llm._asked.clear()
        strategy_llm._cursor = 0
        queue = strategy_llm._queue(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'], context_of=contexts.get)
        assert queue[0] == 'SOLUSDT'

        # Разобрана 40 минут назад при окне 60: обычная пара ждёт, срочная — нет.
        import time
        strategy_llm._asked['SOLUSDT'] = time.time() - 40 * 60
        strategy_llm._asked['BTCUSDT'] = time.time() - 40 * 60
        monkeypatch.setattr(strategy_llm.config, 'LLM_REASK_AFTER_MIN', 60)
        queue = strategy_llm._queue(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'], context_of=contexts.get)
        assert queue == ['SOLUSDT', 'ETHUSDT']
        strategy_llm._asked.clear()

    def test_without_contexts_the_round_robin_is_unchanged(self):
        strategy_llm._asked.clear()
        strategy_llm._cursor = 1
        assert strategy_llm._queue(['A', 'B', 'C'], context_of=lambda p: None) == ['B', 'C', 'A']
        strategy_llm._cursor = 0
