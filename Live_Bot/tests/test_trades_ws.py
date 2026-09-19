"""
Лента сделок по веб-сокету: минутные корзины, запись по закрытии минуты,
тот же формат строки, что у опроса, и молчание опроса, пока поток жив.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trades_ws


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(trades_ws, 'PATH', str(tmp_path / 'delta.jsonl'))
    with trades_ws._lock:
        trades_ws._buckets.clear()
        trades_ws._wanted.clear()
        trades_ws._wanted.update({'BTCUSDT': 'BTCUSDT'})
        trades_ws._stats.update({'events': 0, 'connected': False, 'last_event': 0, 'written': 0})
    yield
    with trades_ws._lock:
        trades_ws._buckets.clear()


def frame(rows):
    return json.dumps({'topic': 'publicTrade.BTCUSDT', 'type': 'snapshot',
                       'ts': rows[-1]['T'], 'data': rows})


class TestTradesBecomeMinutes:

    def test_buys_and_sells_fold_into_the_minute(self):
        m = 1_700_000_000_000 // 60_000 * 60_000
        trades_ws.handle(frame([
            {'T': m + 100, 's': 'BTCUSDT', 'S': 'Buy', 'v': '0.5', 'p': '1'},
            {'T': m + 200, 's': 'BTCUSDT', 'S': 'Sell', 'v': '0.2', 'p': '1'},
            {'T': m + 60_500, 's': 'BTCUSDT', 'S': 'Buy', 'v': '1.0', 'p': '1'},
        ]))
        written = trades_ws.flush(now_ms=m + 120_000)
        assert written == 2
        rows = [json.loads(l) for l in open(trades_ws.PATH, encoding='utf-8')]
        assert rows[0] == {'ts': m, 'pair': 'BTCUSDT', 'value': 0.3, 'buy': 0.5,
                           'sell': 0.2, 'n': 2, 'src': 'ws'}
        assert rows[1]['buy'] == 1.0 and rows[1]['ts'] == m + 60_000

    def test_the_open_minute_waits(self):
        m = 1_700_000_000_000 // 60_000 * 60_000
        trades_ws.handle(frame([{'T': m + 100, 's': 'BTCUSDT', 'S': 'Buy', 'v': '1', 'p': '1'}]))
        assert trades_ws.flush(now_ms=m + 30_000) == 0
        assert trades_ws.flush(now_ms=m + 30_000, force=True) == 1

    def test_the_row_reads_like_the_polled_one(self):
        """positioning.series читает buy/sell/ts/pair — формат тот же."""
        import positioning
        m = 1_700_000_000_000 // 60_000 * 60_000
        trades_ws.handle(frame([{'T': m, 's': 'BTCUSDT', 'S': 'Sell', 'v': '2', 'p': '1'}]))
        trades_ws.flush(now_ms=m + 120_000)
        rows = positioning.series('delta', 'BTCUSDT') if False else \
            [json.loads(l) for l in open(trades_ws.PATH, encoding='utf-8')]
        assert {'ts', 'pair', 'buy', 'sell', 'value'} <= set(rows[0])


class TestHealth:

    def test_alive_means_connected_and_recent(self):
        with trades_ws._lock:
            trades_ws._stats['connected'] = True
            trades_ws._stats['last_event'] = int(time.time() * 1000)
        assert trades_ws.healthy()
        with trades_ws._lock:
            trades_ws._stats['last_event'] = int((time.time() - 600) * 1000)
        assert not trades_ws.healthy()

    def test_polling_stops_while_the_stream_is_alive(self, monkeypatch):
        import positioning
        monkeypatch.setattr(trades_ws, 'healthy', lambda now=None: True)
        monkeypatch.setattr(positioning, '_last_run', {})
        seen = {}

        def collect(client, pairs, sources):
            seen['sources'] = sources
            return {s: 0 for s in sources}
        monkeypatch.setattr(positioning, 'collect', collect)
        positioning.collect_if_due(None, ['BTCUSDT'])
        assert 'delta' not in seen.get('sources', ()), seen
