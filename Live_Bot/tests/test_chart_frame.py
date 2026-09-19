"""
График плана модели в Telegram должен читаться: таймфрейм выбирается по
геометрии плана, окно подрезается под его размах, а при отказе биржи
рисуются часовые свечи, что на руках.
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chart_frame  # noqa: E402


def signal(entry, stop, targets, trigger_level=None):
    return {
        'trading_pair': 'BTCUSDT',
        'setup': {'type': 'LONG' if stop < entry else 'SHORT'},
        'params': {'entry': entry, 'stop_loss': stop, 'take_profit_1': targets[0],
                   'tp_targets': list(targets), 'rr': 2.5},
        'llm': {'trigger_when': 'now', 'trigger_level': trigger_level, 'levels': []},
    }


def candles(n, start, step, tf_minutes=60):
    closes = start + np.arange(n) * step
    return pd.DataFrame({
        'timestamp': pd.to_datetime(np.arange(n) * tf_minutes * 60_000, unit='ms'),
        'open': closes, 'high': closes + 0.2, 'low': closes - 0.2,
        'close': closes, 'volume': np.ones(n),
    })


class TestWhichFrame:
    def test_near_and_tight_plan_gets_quarter_hours(self):
        # Вход в 0.5% от цены, стоп 1.5%, цели 3% и 4.5%: всё в 6%.
        tf, bars = chart_frame.pick(signal(100.0, 98.5, [103.0, 104.5]), price=100.5)
        assert tf == '15m' and bars == chart_frame.FRAMES['15m']

    def test_far_entry_gets_four_hours(self):
        tf, _ = chart_frame.pick(signal(100.0, 98.5, [103.0]), price=104.0)
        assert tf == '4h'

    def test_wide_plan_gets_four_hours_even_when_price_is_at_entry(self):
        tf, _ = chart_frame.pick(signal(100.0, 97.0, [106.0, 110.0]), price=100.0)
        assert tf == '4h'

    def test_trigger_level_counts_as_a_point_to_show(self):
        tf, _ = chart_frame.pick(signal(100.0, 98.5, [103.0], trigger_level=108.0), price=100.0)
        assert tf == '4h'

    def test_no_plan_means_hours(self):
        assert chart_frame.pick({'params': {}}, price=100.0) == ('1h', 120)


class TestTheWindowFitsThePlan:
    def test_a_month_of_trend_is_cut_to_the_plan_scale(self):
        # Свечи прошли 100 → 200, план занимает 100..110: оставить всё —
        # значит сжать план в десятую часть картинки.
        df = candles(180, 100.0, 100.0 / 180)
        out = chart_frame.fit_window(df, [100.0, 98.0, 110.0])
        assert chart_frame.fit_window.__defaults__[0] <= len(out) < 180
        span = float(out['high'].max() - out['low'].min())
        assert span <= 12.0 * 3.0 + 1

    def test_a_quiet_range_is_kept_whole(self):
        df = candles(180, 100.0, 0.01)
        out = chart_frame.fit_window(df, [100.0, 98.0, 110.0])
        assert len(out) == 180


class TestTheMessageUsesTheChosenFrame:
    def test_frames_are_asked_for_the_picked_timeframe(self, monkeypatch):
        import telegram_notify as tg
        import chart_generator
        asked = []
        drawn = {}
        monkeypatch.setattr(tg, '_allowed', lambda e: True)
        monkeypatch.setattr(tg, '_send_photo', lambda path, caption=None, chat_id=None: drawn.setdefault('caption', caption) or True)
        monkeypatch.setattr(chart_generator, 'generate_trade_chart',
                            lambda sig, df, timeframe='1h': drawn.setdefault('tf', timeframe) or 'x.png')

        def frames(pair, tf, limit):
            asked.append((pair, tf, limit))
            return candles(limit, 104.0, 0.0, tf_minutes=240)

        df_1h = candles(120, 104.0, 0.0)
        assert tg.llm_setup_found(signal(100.0, 98.5, [103.0]), df_1h, frames=frames) is True
        assert asked == [('BTCUSDT', '4h', chart_frame.FRAMES['4h'])]
        assert drawn['tf'] == '4h' and '4-часовые' in drawn['caption']

    def test_exchange_refusal_falls_back_to_the_hours_at_hand(self, monkeypatch):
        import telegram_notify as tg
        import chart_generator
        drawn = {}
        monkeypatch.setattr(tg, '_allowed', lambda e: True)
        monkeypatch.setattr(tg, '_send_photo', lambda path, caption=None, chat_id=None: drawn.setdefault('caption', caption) or True)
        monkeypatch.setattr(chart_generator, 'generate_trade_chart',
                            lambda sig, df, timeframe='1h': drawn.setdefault('tf', timeframe) or 'x.png')

        def frames(pair, tf, limit):
            raise RuntimeError('биржа молчит')

        assert tg.llm_setup_found(signal(100.0, 98.5, [103.0]), candles(120, 104.0, 0.0), frames=frames) is True
        assert drawn['tf'] == '1h' and 'часовые свечи' in drawn['caption']


class TestTheChartIsDrawn:
    def test_the_title_names_the_timeframe_and_the_file_exists(self):
        import pytest
        pytest.importorskip('mplfinance')
        import chart_generator
        df = candles(192, 100.0, 0.01, tf_minutes=15)
        path = chart_generator.generate_trade_chart(signal(100.5, 99.0, [103.0, 104.5]), df, timeframe='15m')
        assert path and os.path.exists(path)
        os.remove(path)
