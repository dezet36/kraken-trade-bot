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
    # Фандинг по умолчанию неизвестен: тесты не читают файлы positioning.
    monkeypatch.setattr(llm_rules, 'funding_rate', lambda pair: None)


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


def scan_one(sample, funding=None, now='2026-10-10T00:00:00'):
    ctx = Ctx(sample)
    return llm_rules.scan(['BTCUSDT'], Gate(), balance=10_000, now_ms=ms(now),
                          context_of=lambda pair: ctx, funding_of=lambda pair: funding)


class TestSelectionOnTopOfTheCore:
    """Снятие ликвидности и «против толпы» — отбор ИИ поверх ядра (27.09.2026)."""

    def test_no_sweep_no_trade(self, rules_mode):
        sample = setup()
        sample['factors']['liquidity_swept'] = False
        sample['sweep'] = None
        assert scan_one(sample) == []

    def test_sweep_factor_decides_over_the_sweep_object(self, rules_mode):
        sample = setup()
        sample['factors']['liquidity_swept'] = False
        assert scan_one(sample) == []
        sample['factors']['liquidity_swept'] = True
        assert len(scan_one(sample)) == 1

    @pytest.mark.parametrize('direction,rate_bp,taken', [
        ('BULLISH', +1.0, False),     # лонг, толпа в лонгах платит
        ('BULLISH', 0.0, False),      # лонг при нуле: до −1 б.п. не дотягивает
        ('BULLISH', -1.0, True),      # лонг, толпа в шортах
        ('BEARISH', +1.0, True),      # шорт при ставке на середине Bybit
        ('BEARISH', 0.0, False),      # шорт, толпа клонится в шорт
        ('BEARISH', -2.0, False),     # шорт, толпа в шортах платит
    ])
    def test_against_the_crowd(self, rules_mode, direction, rate_bp, taken):
        out = scan_one(setup(direction), funding=rate_bp / 1e4)
        assert (len(out) == 1) is taken

    def test_unknown_funding_does_not_block(self, rules_mode):
        out = scan_one(setup('BULLISH'), funding=None)
        assert len(out) == 1 and out[0]['signal']['llm']['funding_bp'] is None

    def test_funding_goes_into_the_signal(self, rules_mode):
        out = scan_one(setup('BEARISH'), funding=1.5 / 1e4)
        assert out[0]['signal']['llm']['funding_bp'] == pytest.approx(1.5)
        assert out[0]['signal']['smc']['funding_bp'] == pytest.approx(1.5)

    def test_rule_is_own_copy_not_smc(self):
        """Порог — в копии ИИ; правило не импортируется из стратегии SMC."""
        import re
        assert llm_rules.DECISION.FUNDING_AGAINST_CROWD is True
        assert llm_rules.DECISION.FUNDING_MAX_BP == -1.0
        source = open(llm_rules.__file__, encoding='utf-8').read()
        assert not re.search(r'^\s*(import|from)\s+strategy_smc\b', source, re.M)


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
