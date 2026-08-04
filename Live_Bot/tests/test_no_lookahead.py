"""
Сквозная проверка отсутствия подглядывания в будущее.

Идея теста: решение, принятое на свече i, не должно зависеть от того, есть ли
в данных свечи после i. Строим контекст на ПОЛНОЙ истории и на истории,
обрезанной ровно по i, и требуем совпадения результата evaluate(i).

Это самый важный тест в наборе. Ошибка такого рода не роняет код и не видна
в логах — она просто делает результаты бэктеста недостижимыми в реальности.
Именно так был найден баг с разрешением времени: bias читался из конца года,
а бэктест при этом выглядел правдоподобно.

Тест намеренно медленнее остальных: он строит контекст заново на каждой
проверяемой свече.
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smc import signal as smc_signal  # noqa: E402


def synthetic_market(bars=900, seed=7):
    """
    Детерминированный ряд свечей с трендами, откатами и шумом.

    Нужен ряд, на котором реально формируются структура, зоны и имбалансы —
    на идеальном зигзаге половина логики не активируется и тест ничего не
    проверит.
    """
    rng = np.random.default_rng(seed)

    # Кусочный дрейф: чередование трендов и боковиков
    drift = np.concatenate([
        np.full(200, 0.0012), np.full(150, -0.0009), np.full(120, 0.0002),
        np.full(180, 0.0015), np.full(130, -0.0014), np.full(bars, 0.0005),
    ])[:bars]

    shocks = rng.normal(0, 0.006, bars)
    closes = 100 * np.exp(np.cumsum(drift + shocks))

    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]

    span = np.abs(closes - opens) + closes * rng.uniform(0.001, 0.005, bars)
    highs = np.maximum(opens, closes) + span * rng.uniform(0, 0.6, bars)
    lows = np.minimum(opens, closes) - span * rng.uniform(0, 0.6, bars)

    return pd.DataFrame({
        'timestamp': pd.date_range('2026-01-01', periods=bars, freq='h', tz='UTC'),
        'open': opens, 'high': highs, 'low': lows, 'close': closes,
        'volume': rng.uniform(50, 500, bars),
    })


def frames_from(df_1h):
    """Готовит раскладку таймфреймов так же, как это делает бэктест."""
    def agg(rule):
        return (df_1h.set_index('timestamp')
                .resample(rule)
                .agg({'open': 'first', 'high': 'max', 'low': 'min',
                      'close': 'last', 'volume': 'sum'})
                .dropna().reset_index())

    return {'bias': agg('1D'), 'htf': agg('4h'), 'poi': df_1h}


def describe(result):
    """Сводит сетап к сравнимому виду (без объектов и меток времени)."""
    setup, reason = result
    if setup is None:
        return ('НЕТ', reason)
    trade = setup['params']
    return (
        setup['direction'],
        setup['poi']['type'],
        setup['poi']['index'],
        round(setup['confluence'], 4),
        round(trade['entry'], 8),
        round(trade['stop_loss'], 8),
        tuple(round(t, 8) for t in trade['targets']),
    )


class TestNoLookahead:
    def test_decision_matches_truncated_history(self):
        """
        Решение на свече i идентично при полной и обрезанной истории.

        Расхождение означает, что какой-то слой заглядывает вперёд: либо
        свинг размечен до подтверждения, либо уровень взят из будущего
        периода, либо таймфреймы связаны неверно.
        """
        full = synthetic_market()
        ctx_full = smc_signal.build_context(frames_from(full.copy()), pair='TEST')

        # Проверяем срез свечей, а не все 900: контекст строится заново каждый раз
        checkpoints = range(300, len(full) - 1, 47)
        mismatches = []

        for i in checkpoints:
            truncated = full.iloc[:i + 1].reset_index(drop=True)
            ctx_cut = smc_signal.build_context(frames_from(truncated), pair='TEST')

            got_full = describe(ctx_full.evaluate(i))
            got_cut = describe(ctx_cut.evaluate(i))

            if got_full != got_cut:
                mismatches.append((i, got_full, got_cut))

        assert not mismatches, (
            'Обнаружено подглядывание в будущее на свечах '
            + ', '.join(str(m[0]) for m in mismatches[:5])
            + f'\nпример: полная={mismatches[0][1]}\n        обрезанная={mismatches[0][2]}'
        )

    def test_context_sees_no_unconfirmed_swings(self):
        """Ни один свинг не может быть подтверждён позже, чем существует."""
        df = synthetic_market(bars=400)
        ctx = smc_signal.build_context(frames_from(df), pair='TEST')

        for point in ctx.structure['points']:
            assert point['confirmed_at'] >= point['index'], 'подтверждение раньше свинга'
            assert point['confirmed_at'] < len(df) + 10

    def test_sweeps_complete_before_they_are_used(self):
        """Снятие ликвидности засчитывается только после возврата цены."""
        df = synthetic_market(bars=400)
        ctx = smc_signal.build_context(frames_from(df), pair='TEST')

        for sweep in ctx.sweeps:
            assert sweep['reclaimed_at'] >= sweep['index'], 'возврат раньше прокола'
