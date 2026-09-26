"""
ИИ в режиме «правила» (llm_rules): свои правила отбора, свой пул, окно решений
ФРС, исполнение из своих правил, и режим планов модели не задет.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import llm_rules  # noqa: E402

H = 3_600_000


def ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def setup(direction='BULLISH'):
    long_ = direction == 'BULLISH'
    return {
        'direction': direction,
        'leg': {'start': {'price': 90.0, 'time': '2026-09-01'}, 'end': {'price': 110.0, 'time': '2026-09-02'},
                'size': 20.0},
        'poi': {'type': 'ORDER_BLOCK', 'top': 100.0, 'bottom': 98.0,
                'invalidation': 97.0 if long_ else 103.0, 'index': 700},
        'confluence': 5.2,
        'factors': {'htf_bias_aligned': True, 'premium_discount': True},
        'sweep': {'source': 'EQL', 'level': 97.5, 'extreme': 97.2},
        'structure': {'type': 'BOS', 'level': 108.0},
        'params': {'entry': 100.0, 'stop_loss': 96.85 if long_ else 103.15,
                   'targets': [115.0, 120.0, 125.0] if long_ else [85.0, 80.0, 75.0],
                   'fractions': [0.25, 0.25, 0.5], 'rr': 6.1, 'rr_first': 4.8, 'rr_final': 7.9,
                   'sl_distance': 3.15, 'position_size': 1.0, 'risk_amount': 100.0,
                   'sl_mode': 'conservative'},
    }


class Gate:
    def __init__(self, busy=(), cooling=()):
        self.busy, self.cooling = set(busy), set(cooling)

    def has_position_or_order(self, pair):
        return pair in self.busy

    def check_cooldown(self, pair):
        return pair not in self.cooling


class Frames(dict):
    pass


class Ctx:
    def __init__(self, result=None, reason='нет активных POI'):
        self.result, self.reason = result, reason
        self.frames = {'poi': [0] * 500}
        self.decisions = []

    def evaluate(self, at_index, balance=10_000.0, decision=None):
        self.decisions.append(decision)
        return (self.result, None) if self.result else (None, self.reason)


@pytest.fixture
def rules_mode(monkeypatch):
    monkeypatch.setattr(config, 'LLM_MODE', 'rules', raising=False)
    monkeypatch.setattr(config, 'LLM_EVENT_DATES', '', raising=False)


class TestSwitch:
    def test_plans_mode_by_default(self, monkeypatch):
        monkeypatch.setattr(config, 'LLM_MODE', 'plans', raising=False)
        assert not llm_rules.enabled()

    def test_rules_mode_on_request(self, rules_mode):
        assert llm_rules.enabled()

    def test_the_copy_covers_every_smc_decision_name(self):
        """Копия полная: ни одно правило решения не берётся молча у SMC."""
        from smc import params
        assert set(vars(llm_rules.DECISION)) == set(params.DECISION)


class TestFedWindow:
    def test_inside_the_window_no_entries(self, rules_mode):
        near = llm_rules.event_near(ms('2026-10-28T18:00:00') - 23 * H)
        assert near is not None and near[0] == '2026-10-28'

    def test_outside_the_window(self, rules_mode):
        assert llm_rules.event_near(ms('2026-10-28T18:00:00') + 25 * H) is None

    def test_dates_from_env_replace_the_calendar(self, monkeypatch):
        monkeypatch.setattr(config, 'LLM_EVENT_DATES', '2027-01-27, 2027-03-17', raising=False)
        assert llm_rules.event_near(ms('2027-03-17T12:00:00'))[0] == '2027-03-17'
        assert llm_rules.event_near(ms('2026-10-28T18:00:00')) is None


class TestScan:
    def test_setup_becomes_a_broker_signal(self, rules_mode):
        ctx = Ctx(setup())
        out = llm_rules.scan(['BTCUSDT'], Gate(), balance=10_000, now_ms=ms('2026-10-10T00:00:00'),
                             context_of=lambda pair: ctx)
        assert len(out) == 1
        signal = out[0]['signal']
        assert signal['strategy'] == 'LLM' and signal['setup']['type'] == 'LONG'
        p = signal['params']
        assert p['tp_targets'] == [115.0, 120.0, 125.0] and p['tp_fractions'] == [0.25, 0.25, 0.5]
        assert p['be_level'] is None and p['breakeven_after_tp'] is False
        assert p['max_same_direction'] == llm_rules.DECISION.MAX_SAME_DIRECTION
        assert signal['llm']['mode'] == 'rules' and 'ордер-блок' in signal['llm']['why']
        # Ядро спрошено СВОИМИ правилами ИИ, а не правилами SMC.
        assert ctx.decisions == [llm_rules.DECISION]

    def test_fed_day_refuses_the_found_setup(self, rules_mode):
        ctx = Ctx(setup())
        out = llm_rules.scan(['BTCUSDT'], Gate(), balance=10_000,
                             now_ms=ms('2026-10-28T18:00:00') - 5 * H, context_of=lambda pair: ctx)
        assert out == []

    def test_only_own_pool(self, rules_mode):
        asked = []
        llm_rules.scan(['ARBUSDT', 'ETHUSDT'], Gate(), balance=10_000, now_ms=ms('2026-10-10T00:00:00'),
                       context_of=lambda pair: asked.append(pair) or Ctx())
        assert asked == ['ETHUSDT']

    def test_busy_and_cooling_pairs_are_skipped(self, rules_mode):
        asked = []
        llm_rules.scan(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'], Gate(busy={'BTCUSDT'}, cooling={'ETHUSDT'}),
                       balance=10_000, now_ms=ms('2026-10-10T00:00:00'),
                       context_of=lambda pair: asked.append(pair) or Ctx())
        assert asked == ['SOLUSDT']


class TestExecutionComesFromOwnRules:
    def test_rules_mode_profile(self, rules_mode):
        import strategy_profile as sp
        r = llm_rules.DECISION
        assert sp.expiry_hours('LLM') == r.PENDING_ORDER_MAX_HOURS
        assert sp.cooldown_hours('LLM') == r.COOLDOWN_HOURS
        assert sp.cost_limit_pct('LLM') == r.MAX_ENTRY_COST_SHARE_PCT
        assert sp.max_hold_hours('LLM') == r.MAX_POSITION_HOLD_HOURS
        assert sp.drops_at_target('LLM') is bool(r.CANCEL_PENDING_AT_TARGET)
        assert sp.fills_through_market('LLM') is bool(r.FILL_THROUGH_MARKET)
        assert sp.min_stop_pct('LLM') == pytest.approx(r.MIN_SL_PCT * 100)

    def test_plans_mode_profile_untouched(self, monkeypatch):
        import strategy_profile as sp
        monkeypatch.setattr(config, 'LLM_MODE', 'plans', raising=False)
        monkeypatch.setattr(config, 'LLM_TRIGGER_TTL_H', 12, raising=False)
        monkeypatch.setattr(config, 'LLM_COOLDOWN_HOURS', 4.0, raising=False)
        assert sp.expiry_hours('LLM') == 12.0 and sp.cooldown_hours('LLM') == 4.0


class TestStrategyRouting:
    def test_rules_mode_does_not_ask_the_model(self, rules_mode, monkeypatch):
        import strategy_llm
        monkeypatch.setattr(llm_rules, 'scan', lambda pairs, gate, client=None, balance=None: ['из правил'])
        monkeypatch.setattr(strategy_llm.llm_local, 'available',
                            lambda: (_ for _ in ()).throw(AssertionError('модель не нужна')))
        assert strategy_llm.scan_for_setups(['BTCUSDT'], Gate()) == ['из правил']
