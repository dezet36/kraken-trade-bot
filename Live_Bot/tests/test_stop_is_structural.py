"""
Стоп стоит там, где ломается идея, и никуда не двигается.

Решение владельца (21.09.2026): минимальный стоп — ФИЛЬТР (тесный стоп не
окупает шум и комиссии — сетап не берётся), а не место стопа. До этого
FIBO, SMC и уровни при тесной структуре ОТОДВИГАЛИ стоп до минимума: он
уезжал с уровня инвалидации на «просто расстояние от цены». У ИИ и
Боллинджера правило соблюдалось и раньше (отказ «стоп теснее минимального»;
'widen' у Боллинджера давал RR<1 и отбрасывался).
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── FIBO ────────────────────────────────────────────────────────────────────

class TestFibo:
    def _params(self, monkeypatch, min_stop):
        import strategy
        # Патчим модуль настроек, который держит САМА стратегия: другие наборы
        # перезагружают settings_store, и свежий import был бы чужим объектом.
        config = strategy.config
        monkeypatch.setattr(strategy.settings, 'min_stop_pct', lambda name: min_stop)
        setup = {'type': 'LONG', 'start_price': 1000.0, 'end_price': 2000.0, 'size': 1000.0}
        entry = setup['end_price'] - setup['size'] * config.ENTRY_RETRACE
        structural = entry - (setup['end_price'] - setup['size'] * config.SL_LEVEL_R
                              - setup['size'] * config.SL_BUFFER)
        return strategy.calculate_trade_params(setup, entry, 10_000, log_reject=False), entry, structural

    def test_the_stop_is_the_invalidation_level_when_wide_enough(self, monkeypatch):
        params, entry, structural = self._params(monkeypatch, min_stop=0.0)
        assert params is not None
        assert entry - params['stop_loss'] == pytest.approx(structural)

    def test_a_tighter_structure_is_refused_not_stretched(self, monkeypatch):
        _, entry, structural = self._params(monkeypatch, min_stop=0.0)
        too_tight = structural / entry * 1.5          # минимум выше структуры
        params, _, _ = self._params(monkeypatch, min_stop=too_tight)
        assert params is None, 'стоп отодвинули вместо отказа'


# ── SMC ─────────────────────────────────────────────────────────────────────

class TestSmc:
    def _context(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_strategy_isolation_behaviour import market, frames_of
        from smc import signal as smc_signal
        df = market(2000)
        return df, smc_signal.build_context(frames_of(df.copy()), pair='TEST')

    def _first_setup(self, ctx, n):
        for i in range(800, n, 3):
            setup, _ = ctx.evaluate(i)
            if setup is not None:
                return i, setup
        pytest.skip('на этом ряду SMC не нашла сетапа')

    def test_the_stop_sits_beyond_the_zone_edge(self):
        from smc import params
        df, ctx = self._context()
        i, setup = self._first_setup(ctx, len(df))
        t = setup['params']
        edge = setup['poi']['invalidation']
        dist = abs(t['entry'] - t['stop_loss'])
        assert dist >= t['entry'] * params.MIN_SL_PCT - 1e-9
        # Стоп — за границей зоны с буфером (conservative без свипа), а не на
        # «минимуме от входа». Свип может отодвинуть его дальше, но не ближе.
        expect = (edge * (1 - params.SL_BUFFER_PCT) if setup['direction'] == 'BULLISH'
                  else edge * (1 + params.SL_BUFFER_PCT))
        if setup['direction'] == 'BULLISH':
            assert t['stop_loss'] <= expect + 1e-9
        else:
            assert t['stop_loss'] >= expect - 1e-9

    def test_a_tight_zone_is_refused_with_a_named_reason(self, monkeypatch):
        from smc import params
        df, ctx = self._context()
        i, setup = self._first_setup(ctx, len(df))
        t = setup['params']
        dist_pct = abs(t['entry'] - t['stop_loss']) / t['entry']
        monkeypatch.setattr(params, 'MIN_SL_PCT', dist_pct * 1.5)
        again, reason = ctx.evaluate(i)
        assert again is None
        assert 'теснее минимума' in reason, reason


# ── Уровни ──────────────────────────────────────────────────────────────────

class TestLevels:
    def _scene(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_levels import flat, touch, series
        rows = flat(80, price=100.0, vol=1.0)
        rows[20] = touch(100.0, 2.0)
        rows[40] = touch(100.0, 2.0)
        rows[50] = (103.3, 99.7, 100.0, 1.0)
        rows[65] = (103.3, 99.7, 100.0, 1.0)
        rows[75] = (98.7, 98.3, 98.4, 1.0)
        rows[76] = (98.6, 97.0, 97.4, 1.0)
        rows[77] = (99.0, 97.3, 98.7, 5.0)
        return series(rows)

    def test_the_stop_is_beyond_the_pierce_extreme_plus_pad(self, monkeypatch):
        from levels import core, params
        monkeypatch.setattr(params, 'MIN_STOP_PCT', 0.0)
        high, low, close, volume = self._scene()
        setup, reason = core.evaluate(high, low, close, volume, 77)
        assert setup is not None, reason
        a = core.atr(high, low, close)
        expect = abs(setup['entry'] - setup['pierce_extreme']) + params.STOP_PAD_ATR * a[77]
        assert setup['sl_distance'] == pytest.approx(expect)

    def test_a_shallow_pierce_is_refused_not_stretched(self, monkeypatch):
        from levels import core, params
        monkeypatch.setattr(params, 'MIN_STOP_PCT', 0.0)
        high, low, close, volume = self._scene()
        setup, _ = core.evaluate(high, low, close, volume, 77)
        natural = setup['sl_distance'] / setup['entry'] * 100
        monkeypatch.setattr(params, 'MIN_STOP_PCT', natural * 1.5)
        setup, reason = core.evaluate(high, low, close, volume, 77)
        assert setup is None
        assert 'теснее минимума' in reason, reason
