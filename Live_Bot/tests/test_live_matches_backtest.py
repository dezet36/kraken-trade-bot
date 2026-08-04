"""
Живой путь и бэктест обязаны принимать ОДНО И ТО ЖЕ решение.

Весь проект держится на инварианте «один и тот же код в бою и в бэктесте».
Но пути к решению разные: бэктест строит контекст на всей истории и зовёт
evaluate(i), а живой бот подгружает свечи, отбрасывает незакрытую и зовёт
evaluate(последняя). Разойтись они могут на чём угодно — на обрезке свечи,
на кэше контекста, на подготовке таймфреймов.

Расхождение здесь означает, что подобранные параметры относятся к стратегии,
которая в бою не работает. Ровно это и случилось с дефектом merge_swings:
бэктест не видел части сломов структуры.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc import signal as smc_signal   # noqa: E402
import strategy_smc                     # noqa: E402


def market(bars=700, seed=11):
    """Ряд с трендами и откатами — на идеальном зигзаге половина логики спит."""
    rng = np.random.default_rng(seed)
    drift = np.concatenate([
        np.full(180, 0.0013), np.full(140, -0.0011),
        np.full(160, 0.0004), np.full(bars, 0.0009),
    ])[:bars]
    closes = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.006, bars)))
    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    span = np.abs(closes - opens) + closes * rng.uniform(0.001, 0.005, bars)
    return pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=bars, freq='h', tz='UTC'),
        'open': opens,
        'high': np.maximum(opens, closes) + span * rng.uniform(0, 0.6, bars),
        'low': np.minimum(opens, closes) - span * rng.uniform(0, 0.6, bars),
        'close': closes,
        'volume': rng.uniform(50, 500, bars),
    })


def frames_of(df_1h):
    def agg(rule):
        return (df_1h.set_index('timestamp').resample(rule)
                .agg({'open': 'first', 'high': 'max', 'low': 'min',
                      'close': 'last', 'volume': 'sum'})
                .dropna().reset_index())
    return {'bias': agg('1D'), 'htf': agg('4h'), 'poi': df_1h}


def brief(setup):
    """Сетап в сравнимом виде — без объектов и меток времени."""
    if setup is None:
        return None
    trade = setup['params']
    return (setup['direction'], setup['poi']['type'], setup['poi']['index'],
            round(setup['confluence'], 6), round(trade['entry'], 8),
            round(trade['stop_loss'], 8),
            tuple(round(t, 8) for t in trade['targets']),
            tuple(round(f, 8) for f in trade['fractions']))


class TestLiveMatchesBacktest:
    def test_same_decision_on_same_bar(self, monkeypatch):
        """
        Бэктест на полной истории и живой путь на истории «до этой свечи
        включительно плюс формирующаяся» дают идентичный сетап.

        Живому боту биржа отдаёт последней НЕЗАКРЫТУЮ свечу, и адаптер её
        отбрасывает. Здесь это воспроизводится буквально: подкладываем на одну
        свечу больше и проверяем, что решение не изменилось.
        """
        full = market()
        ctx_full = smc_signal.build_context(frames_of(full.copy()), pair='TEST')

        mismatches = []
        for i in range(200, len(full) - 1, 31):
            # Живой бот: свечи по i включительно + незакрытая i+1
            visible = full.iloc[:i + 2].reset_index(drop=True)

            def fake_fetch(timeframe, limit=None, symbol=None, client=None, _v=visible):
                if timeframe == '1h':
                    return _v.copy()
                rule = {'1d': '1D', '4h': '4h'}[timeframe]
                return (_v.set_index('timestamp').resample(rule)
                        .agg({'open': 'first', 'high': 'max', 'low': 'min',
                              'close': 'last', 'volume': 'sum'})
                        .dropna().reset_index())

            monkeypatch.setattr(strategy_smc, 'fetch_ohlcv', fake_fetch)
            strategy_smc._context_cache.clear()

            live_ctx = strategy_smc.get_context('TEST')
            live = None
            if live_ctx is not None:
                last = len(live_ctx.frames['poi']) - 1
                live = brief(live_ctx.evaluate(last)[0])

            backtest = brief(ctx_full.evaluate(i)[0])
            if live != backtest:
                mismatches.append((i, backtest, live))

        assert not mismatches, (
            'Живой путь разошёлся с бэктестом на свечах '
            + ', '.join(str(m[0]) for m in mismatches[:5])
            + f'\nбэктест={mismatches[0][1]}\nживой  ={mismatches[0][2]}')

    def test_forming_candle_is_dropped(self):
        """
        Незакрытая свеча не должна участвовать в разметке: её high/low ещё
        меняются, и структура «прыгала» бы на каждом тике.
        """
        df = market(bars=100)

        assert len(strategy_smc._drop_forming_candle(df)) == len(df) - 1
        assert strategy_smc._drop_forming_candle(df).iloc[-1]['close'] == \
            df.iloc[-2]['close']
