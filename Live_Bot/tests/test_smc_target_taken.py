"""
SMC и сетап, у которого цена уже за первой целью.

25.09.2026 SMC предлагала лонги LTC и LINK на 3.5R и 6.8R за входом, и брокер
снимал такие заявки на следующей свече («цена дошла до цели без нас»). Было
предложено не выдавать такие сетапы (smc/params.SKIP_TARGET_TAKEN) — бэктест
показал, что это вредно: такие сетапы потом наливаются на откате и дают
лучшие сделки стратегии. Правило оставлено выключенным, а у SMC снято само
снятие заявки у цели (smc/params.CANCEL_PENDING_AT_TARGET,
strategy_profile.drops_at_target).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _context():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_strategy_isolation_behaviour import market, frames_of
    from smc import signal as smc_signal
    df = market(2000)
    return df, smc_signal.build_context(frames_of(df.copy()), pair='TEST')


def _first_setup(ctx, n):
    for i in range(800, n, 3):
        setup, _ = ctx.evaluate(i)
        if setup is not None:
            return i, setup
    pytest.skip('на этом ряду SMC не нашла сетапа')


def _beyond_target(ctx, i, setup):
    first = setup['params']['targets'][0]
    frame = ctx.frames['poi']
    frame.iloc[i, frame.columns.get_loc('close')] = (
        first * (1.001 if setup['direction'] == 'BULLISH' else 0.999))


class TestTheSkipRuleIsOffAndWorksIfSwitchedOn:

    def test_it_is_off(self):
        from smc import params
        assert params.SKIP_TARGET_TAKEN is False, 'по бэктесту правило вредно — см. smc/params'

    def test_a_setup_beyond_its_target_is_still_offered(self):
        df, ctx = _context()
        i, setup = _first_setup(ctx, len(df))
        _beyond_target(ctx, i, setup)
        assert ctx.evaluate(i)[0] is not None

    def test_switched_on_it_refuses_with_a_named_reason(self, monkeypatch):
        from smc import params
        df, ctx = _context()
        i, setup = _first_setup(ctx, len(df))
        _beyond_target(ctx, i, setup)
        monkeypatch.setattr(params, 'SKIP_TARGET_TAKEN', True)
        again, reason = ctx.evaluate(i)
        assert again is None and reason == 'цена уже за первой целью'


class TestSmcOrdersWaitPastTheTarget:

    def test_smc_does_not_drop_others_do(self):
        import strategy_profile
        assert strategy_profile.drops_at_target('SMC') is False
        for other in ('FIBO', 'LEVELS', 'RSIBB', 'LLM'):
            assert strategy_profile.drops_at_target(other) is True, other

    def test_it_is_a_decision_of_smc_not_structure(self):
        from smc import params
        for name in ('SKIP_TARGET_TAKEN', 'CANCEL_PENDING_AT_TARGET'):
            assert name in params.DECISION and name not in params.STRUCTURAL
