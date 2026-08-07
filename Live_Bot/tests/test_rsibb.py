"""
Скальпинг по RSI и Боллинджеру: индикаторы, режимы чтения RSI и геометрия.

ЗАЧЕМ ЭТИ ТЕСТЫ ИМЕННО ЗДЕСЬ. Индикаторы считаются один раз на всю серию, а
решение принимается на отдельном баре. Это быстро, но открывает ровно одну
дыру: скользящее окно, посчитанное по всей серии, может незаметно смотреть
вперёд, и замер тогда даёт результат, невоспроизводимый в бою.

Второе — три режима чтения RSI. Канонический и обратный отличаются знаком
одного сравнения, и перепутать их — значит померить противоположное тому, что
написано в отчёте, не получив ни одной ошибки.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rsibb import core  # noqa: E402


def series(n=400, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.002, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.002, n)))
    return close, high, low, close


class TestNoLookahead:
    def test_indicators_do_not_change_when_future_is_added(self):
        open_, high, low, close = series()
        cut = 300
        early = core.indicators(open_[:cut], high[:cut], low[:cut], close[:cut])
        full = core.indicators(open_, high, low, close)
        for name in ('mid', 'upper', 'lower', 'rsi', 'adx'):
            a, b = early[name], full[name][:cut]
            both = np.isfinite(a) & np.isfinite(b)
            assert both.any(), f'{name}: сравнивать нечего'
            assert np.allclose(a[both], b[both], rtol=1e-9), name

    def test_warmup_is_marked_not_guessed(self):
        """
        Прогрев обязан быть NaN, а не правдоподобным числом.

        Индикатор, выдающий на десятом баре «значение» вместо NaN, не падает —
        он просто торгует по мусору первые несколько дней каждого прогона.
        """
        open_, high, low, close = series()
        ind = core.indicators(open_, high, low, close)
        assert np.isnan(ind['mid'][:19]).all()
        assert np.isnan(ind['rsi'][:14]).all()
        assert np.isnan(ind['adx'][:28]).all()


class TestBollinger:
    def test_bands_are_symmetric_around_mid(self):
        open_, high, low, close = series()
        mid, upper, lower = core.bollinger(close, period=20, mult=2.0)
        ok = np.isfinite(mid)
        assert np.allclose(upper[ok] - mid[ok], mid[ok] - lower[ok])

    def test_constant_price_gives_zero_width(self):
        flat = np.full(60, 100.0)
        mid, upper, lower = core.bollinger(flat, period=20, mult=2.0)
        assert upper[-1] == pytest.approx(100.0)
        assert lower[-1] == pytest.approx(100.0)


class TestRsi:
    def test_only_rising_closes_give_hundred(self):
        rising = np.arange(1, 80, dtype=float)
        assert core.rsi(rising, period=14)[-1] == pytest.approx(100.0)

    def test_only_falling_closes_give_zero(self):
        falling = np.arange(80, 1, -1, dtype=float)
        assert core.rsi(falling, period=14)[-1] == pytest.approx(0.0, abs=1e-6)


def _at_lower_band(rsi_value):
    """Искусственный бар: цена задела нижнюю полосу, RSI задан вручную."""
    size = 40
    ind = {
        'open': np.full(size, 100.0), 'high': np.full(size, 101.0),
        'low': np.full(size, 99.0), 'close': np.full(size, 100.0),
        'mid': np.full(size, 100.0), 'upper': np.full(size, 102.0),
        'lower': np.full(size, 99.0), 'width': np.full(size, 3.0),
        'width_ratio': np.full(size, 1.0),
        'rsi': np.full(size, rsi_value), 'adx': np.full(size, 15.0),
    }
    return ind


class TestRsiModes:
    """
    Канон и обратное прочтение отличаются знаком одного сравнения. Перепутать
    их — значит померить противоположное тому, что написано в отчёте.
    """

    def test_extreme_wants_oversold_for_long(self):
        setup, why = core.evaluate(_at_lower_band(25), 30, rsi_mode='extreme',
                                   rsi_low=30, rsi_high=70)
        assert setup and setup['direction'] == 'LONG'
        setup, why = core.evaluate(_at_lower_band(60), 30, rsi_mode='extreme',
                                   rsi_low=30, rsi_high=70)
        assert setup is None and 'перепроданность' in why

    def test_divergence_wants_the_opposite(self):
        setup, _why = core.evaluate(_at_lower_band(60), 30,
                                    rsi_mode='divergence',
                                    rsi_low=50, rsi_high=50)
        assert setup and setup['direction'] == 'LONG'
        setup, why = core.evaluate(_at_lower_band(25), 30,
                                   rsi_mode='divergence',
                                   rsi_low=50, rsi_high=50)
        assert setup is None and 'импульс вниз' in why

    def test_neutral_wants_the_middle(self):
        assert core.evaluate(_at_lower_band(50), 30, rsi_mode='neutral',
                             rsi_low=40, rsi_high=60)[0]
        assert core.evaluate(_at_lower_band(25), 30, rsi_mode='neutral',
                             rsi_low=40, rsi_high=60)[0] is None

    def test_off_skips_the_check(self):
        assert core.evaluate(_at_lower_band(50), 30, rsi_mode='off')[0]

    def test_unknown_mode_refuses_instead_of_trading(self):
        setup, why = core.evaluate(_at_lower_band(25), 30, rsi_mode='???')
        assert setup is None and 'неизвестный режим' in why


class TestFilters:
    def test_adx_blocks_trend(self):
        ind = _at_lower_band(25)
        ind['adx'] = np.full(40, 35.0)
        setup, why = core.evaluate(ind, 30, rsi_mode='extreme', adx_max=25)
        assert setup is None and 'тренд' in why
        assert core.evaluate(ind, 30, rsi_mode='extreme', adx_max=0)[0]

    def test_widening_bands_blocked(self):
        ind = _at_lower_band(25)
        ind['width_ratio'] = np.full(40, 1.5)
        setup, why = core.evaluate(ind, 30, rsi_mode='extreme',
                                   max_width_ratio=1.1)
        assert setup is None and 'расширяются' in why


class TestGeometry:
    def test_long_trade_from_lower_band(self):
        setup, _ = core.evaluate(_at_lower_band(25), 30, rsi_mode='extreme')
        trade = core.build_trade(setup, target_frac=1.0, stop_frac=0.5,
                                 min_stop_pct=0.0, min_rr=0.0)
        assert trade['entry'] == pytest.approx(99.0)      # нижняя полоса
        assert trade['target'] == pytest.approx(100.5)    # +полуширина 1.5
        assert trade['stop'] == pytest.approx(98.25)      # −0.5 полуширины
        assert trade['rr'] == pytest.approx(2.0)

    def test_stop_floor_widens_and_lowers_rr(self):
        """Пол по стопу — арифметика издержек, и он обязан менять RR."""
        setup, _ = core.evaluate(_at_lower_band(25), 30, rsi_mode='extreme')
        wide = core.build_trade(setup, stop_frac=0.5, min_stop_pct=2.0,
                                min_rr=0.0)
        assert wide['stop'] == pytest.approx(99.0 - 99.0 * 0.02)
        assert wide['rr'] < 2.0

    def test_min_rr_rejects_instead_of_returning_bad_geometry(self):
        setup, _ = core.evaluate(_at_lower_band(25), 30, rsi_mode='extreme')
        assert core.build_trade(setup, stop_frac=2.0, min_rr=1.0,
                                min_stop_pct=0.0) is None
