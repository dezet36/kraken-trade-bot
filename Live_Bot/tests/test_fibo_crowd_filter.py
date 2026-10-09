"""
Фибо «против толпы» (27.09.2026): сканер фибо не берёт сетап, если толпа стоит
в сторону сделки — у лонга ставка фандинга > порога, у шорта < минус порога.
Нет свежей ставки — отказа нет. Замер — research/fibo_crowd.py.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import pair_scanner  # noqa: E402


@pytest.fixture(autouse=True)
def filter_on(monkeypatch):
    monkeypatch.setattr(config, 'FIBO_FUNDING_AGAINST_CROWD', True)
    monkeypatch.setattr(config, 'FIBO_FUNDING_MAX_BP', 0.0)


class TestCrowdReason:
    @pytest.mark.parametrize('direction, rate, blocked', [
        ('LONG', +0.0001, True),
        ('LONG', -0.0001, False),
        ('LONG', 0.0, False),
        ('SHORT', -0.0001, True),
        ('SHORT', +0.0001, False),
    ])
    def test_sides(self, direction, rate, blocked):
        assert bool(pair_scanner.crowd_reason(direction, rate)) is blocked

    def test_no_rate_no_refusal(self):
        assert pair_scanner.crowd_reason('LONG', None) is None

    def test_switch_off(self, monkeypatch):
        monkeypatch.setattr(config, 'FIBO_FUNDING_AGAINST_CROWD', False)
        assert pair_scanner.crowd_reason('LONG', +0.001) is None

    def test_own_copy_not_smc(self):
        """Правило у фибо своё: сканер не импортирует стратегию SMC (изоляция)."""
        import inspect
        src = inspect.getsource(pair_scanner)
        assert 'strategy_smc' not in src and 'smc.params' not in src


class TestScanner:
    def _patch(self, monkeypatch, direction, rate):
        df = pd.DataFrame({'open': [100.0] * 60, 'high': [101.0] * 60, 'low': [99.0] * 60,
                           'close': [108.0] * 60 if direction == 'LONG' else [92.0] * 60,
                           'volume': [1.0] * 60})
        if direction == 'LONG':
            setup = {'type': 'LONG', 'end_price': 110.0, 'size': 10.0, 'start_price': 100.0}
            zones = ({'top': 106.18, 'bottom': 103.82, 'name': 'A'}, {'top': 101.0, 'bottom': 100.0, 'name': 'B'})
            trend = 'BULLISH'
        else:
            setup = {'type': 'SHORT', 'end_price': 90.0, 'size': 10.0, 'start_price': 100.0}
            zones = ({'top': 96.18, 'bottom': 93.82, 'name': 'A'}, {'top': 100.0, 'bottom': 99.0, 'name': 'B'})
            trend = 'BEARISH'
        monkeypatch.setattr(pair_scanner, 'fetch_ohlcv', lambda *a, **k: df)
        monkeypatch.setattr(pair_scanner, 'find_recent_impulse', lambda *a, **k: setup)
        monkeypatch.setattr(pair_scanner, 'get_zones', lambda s: zones)
        monkeypatch.setattr(pair_scanner, 'get_htf_trend', lambda *a, **k: (trend, 0.5))
        monkeypatch.setattr(pair_scanner, 'calculate_trade_params', lambda *a, **k: {'rr': 2.0})
        from data import positioning
        monkeypatch.setattr(positioning, 'latest', lambda source, pair, **k: rate if source == 'funding' else None)

    class TM:
        def check_cooldown(self, pair):
            return True

    def test_long_with_the_crowd_is_skipped(self, monkeypatch):
        self._patch(monkeypatch, 'LONG', +0.0001)
        assert pair_scanner.scan_for_setups(['BTCUSDT'], self.TM()) == []

    def test_short_against_the_crowd_passes_with_rate(self, monkeypatch):
        self._patch(monkeypatch, 'SHORT', +0.0001)
        out = pair_scanner.scan_for_setups(['BTCUSDT'], self.TM())
        assert len(out) == 1 and out[0]['funding_bp'] == pytest.approx(1.0)

    def test_unknown_rate_passes(self, monkeypatch):
        self._patch(monkeypatch, 'LONG', None)
        out = pair_scanner.scan_for_setups(['BTCUSDT'], self.TM())
        assert len(out) == 1 and out[0]['funding_bp'] is None

    def test_rate_is_asked_with_the_exchange_client(self, monkeypatch):
        """Ставка на момент решения: клиент биржи доходит до общего слоя (01.10.2026)."""
        self._patch(monkeypatch, 'SHORT', None)
        from data import positioning
        seen = []
        monkeypatch.setattr(positioning, 'settled_funding',
                            lambda pair, client=None, **k: seen.append((pair, client)) or 0.0001)
        client = object()
        out = pair_scanner.scan_for_setups(['BTCUSDT'], self.TM(), client=client)
        assert seen == [('BTCUSDT', client)]
        assert len(out) == 1 and out[0]['funding_bp'] == pytest.approx(1.0)
