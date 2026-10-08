"""
SMC: свой пул пар и фильтр «против толпы» (27.09.2026).

Фильтр: SMC берёт сетап, только если толпа не стоит в сторону сделки — у лонга
ставка фандинга ≤ порога, у шорта ≥ минус порога. Нет свежей ставки — отказа
нет (торговля от источника не зависит). Пул: вне smc/params.TRADE_POOL SMC
пары не разбирает. Замеры — docs/SMC_исследование_2026-09.md.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_smc  # noqa: E402
from smc import params  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_strategy_smc_adapter import make_setup  # noqa: E402


@pytest.fixture(autouse=True)
def filter_on(monkeypatch):
    monkeypatch.setattr(params, 'FUNDING_AGAINST_CROWD', True)
    monkeypatch.setattr(params, 'FUNDING_MAX_BP', 0.0)


class TestCrowdReason:
    @pytest.mark.parametrize('direction, rate, blocked', [
        ('BULLISH', +0.0001, True),    # лонг, лонги платят — толпа в лонгах
        ('BULLISH', -0.0001, False),   # лонг, шорты платят — толпа против
        ('BULLISH', 0.0, False),       # ноль — не за сделку
        ('BEARISH', -0.0001, True),    # шорт, шорты платят — толпа в шортах
        ('BEARISH', +0.0001, False),   # шорт, лонги платят — толпа против
        ('LONG', +0.0001, True),       # сторона словами бота — то же
        ('SHORT', -0.0001, True),
    ])
    def test_sides(self, direction, rate, blocked):
        reason = strategy_smc.crowd_reason(direction, rate)
        assert bool(reason) is blocked
        if blocked:
            assert reason.startswith('толпа за сделку')

    def test_no_rate_no_refusal(self):
        """Нет ставки — нет отказа: источник не решает, торговать ли (CLAUDE.md, «Данные»)."""
        assert strategy_smc.crowd_reason('BULLISH', None) is None

    def test_switch_off(self, monkeypatch):
        monkeypatch.setattr(params, 'FUNDING_AGAINST_CROWD', False)
        assert strategy_smc.crowd_reason('BULLISH', +0.001) is None

    def test_threshold_is_signed_toward_the_trade(self, monkeypatch):
        monkeypatch.setattr(params, 'FUNDING_MAX_BP', -1.0)
        assert strategy_smc.crowd_reason('BULLISH', -0.00005)        # −0.5 б.п. > −1 — отказ
        assert strategy_smc.crowd_reason('BULLISH', -0.0002) is None  # −2 б.п. — толпа сильно против
        assert strategy_smc.crowd_reason('BEARISH', +0.0002) is None


class TestAnalyzeMarket:
    def _ctx(self, setup):
        import pandas as pd

        class Ctx:
            frames = {'poi': pd.DataFrame({'close': [1.0, 2.0]})}

            def evaluate(self, index, balance=None):
                return setup, None
        return Ctx()

    def _patch(self, monkeypatch, setup, rate):
        monkeypatch.setattr(strategy_smc, 'get_context', lambda pair, client=None: self._ctx(setup))
        monkeypatch.setattr(strategy_smc, '_apply_settings', lambda: None)
        import positioning
        monkeypatch.setattr(positioning, 'latest', lambda source, pair, **k: rate if source == 'funding' else None)

    def test_crowd_with_the_trade_is_refused_with_a_reason(self, monkeypatch):
        self._patch(monkeypatch, make_setup('BULLISH'), +0.0001)
        import shadow
        monkeypatch.setattr(shadow, 'watch', lambda *a, **k: None)
        assert strategy_smc.analyze_market('BTCUSDT', risk_scale=1.0) is None
        assert strategy_smc._last_reason['BTCUSDT'].startswith('толпа за сделку')

    def test_the_refused_setup_goes_to_the_shadows(self, monkeypatch):
        """Отказ по толпе — в тени: чем кончился бы сетап, пишется вживую."""
        self._patch(monkeypatch, make_setup('BULLISH'), +0.0001)
        import shadow
        seen = []
        monkeypatch.setattr(shadow, 'watch', lambda strategy, signal, gate, detail='', **k:
                            seen.append((strategy, signal['setup']['type'], signal['params']['entry'], gate)))
        strategy_smc.analyze_market('BTCUSDT', risk_scale=1.0)
        assert seen == [('SMC', 'LONG', 101.0, 'толпа за сделку')]

    def test_a_broken_shadow_does_not_break_the_scan(self, monkeypatch):
        self._patch(monkeypatch, make_setup('BULLISH'), +0.0001)
        import shadow

        def boom(*a, **k):
            raise RuntimeError('диск')
        monkeypatch.setattr(shadow, 'watch', boom)
        assert strategy_smc.analyze_market('BTCUSDT', risk_scale=1.0) is None

    def test_crowd_against_passes_and_rate_goes_to_the_journal(self, monkeypatch):
        self._patch(monkeypatch, make_setup('BEARISH', targets=(90.0, 85.0, 80.0)), +0.0001)
        signal = strategy_smc.analyze_market('BTCUSDT', risk_scale=1.0)
        assert signal is not None
        assert signal['smc']['funding_bp'] == pytest.approx(1.0)

    def test_unknown_rate_does_not_block(self, monkeypatch):
        self._patch(monkeypatch, make_setup('BULLISH'), None)
        signal = strategy_smc.analyze_market('BTCUSDT', risk_scale=1.0)
        assert signal is not None
        assert signal['smc']['funding_bp'] is None

    def test_rate_is_asked_with_the_exchange_client(self, monkeypatch):
        """
        Ставка — на момент решения: клиент биржи доходит до общего слоя, чтобы
        выплату, совпавшую с закрытием часа, не ждать от сборщика (01.10.2026).
        """
        self._patch(monkeypatch, make_setup('BEARISH', targets=(90.0, 85.0, 80.0)), None)
        import positioning
        seen = []
        monkeypatch.setattr(positioning, 'settled_funding',
                            lambda pair, client=None, **k: seen.append((pair, client)) or 0.0001)
        client = object()
        signal = strategy_smc.analyze_market('BTCUSDT', client=client, risk_scale=1.0)
        assert seen == [('BTCUSDT', client)]
        assert signal['smc']['funding_bp'] == pytest.approx(1.0)


class TestPool:
    def test_pairs_outside_the_pool_are_not_scanned(self, monkeypatch):
        seen = []
        monkeypatch.setattr(params, 'TRADE_POOL', ('BTCUSDT',))
        monkeypatch.setattr(strategy_smc, 'market_regime', lambda client=None: ('RANGE', 1.0, 'тест'))
        monkeypatch.setattr(strategy_smc, 'analyze_market',
                            lambda pair, client=None, risk_scale=None: seen.append(pair))

        class TM:
            def check_cooldown(self, pair):
                return True

            def has_position_or_order(self, pair):
                return False

        strategy_smc.scan_for_setups(['BTCUSDT', 'BICOUSDT', 'ZECUSDT'], TM())
        assert seen == ['BTCUSDT']

    def test_default_pool_is_the_measured_ten(self):
        """Пул — ровно те 10 пар, на которых мерилась стратегия (research/backtest_smc)."""
        if os.getenv('SMC_TRADE_POOL'):
            pytest.skip('пул переопределён окружением')
        assert set(params.TRADE_POOL) == {
            'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT',
            'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT'}

    def test_default_threshold_is_the_bybit_midpoint(self):
        """Порог по умолчанию — −1 б.п.: середина ставки Bybit +1 б.п. (docs/SMC_исследование_2026-09.md)."""
        if os.getenv('SMC_FUNDING_MAX_BP'):
            pytest.skip('порог переопределён окружением')
        import params_env
        _f, _i, _b, _s = params_env.reader('SMC')
        assert _f('FUNDING_MAX_BP', -1.0) == -1.0
        src = open(params.__file__, encoding='utf-8').read()
        assert "FUNDING_MAX_BP = _f('FUNDING_MAX_BP', -1.0)" in src

    def test_new_names_are_decisions_not_structure(self):
        for name in ('TRADE_POOL', 'FUNDING_AGAINST_CROWD', 'FUNDING_MAX_BP'):
            assert name in params.DECISION and name not in params.STRUCTURAL

    def test_ai_rules_copy_is_its_own(self):
        """
        У ИИ своя копия правил (llm_rules.DECISION): включать ли там фильтр толпы
        и с каким порогом — решение ИИ (27.09.2026 включён, −1 б.п.). Этот тест
        держит только изоляцию: копия — отдельный объект, пул SMC ей не нужен,
        и модуль ИИ не импортирует адаптер SMC.
        """
        import llm_rules
        assert llm_rules.DECISION is not params
        assert llm_rules.DECISION.TRADE_POOL == ()
        src = open(llm_rules.__file__, encoding='utf-8').read()
        assert 'import strategy_smc' not in src and 'from strategy_smc' not in src

    def test_smc_threshold_change_does_not_reach_the_ai_copy(self, monkeypatch):
        """Правка порога SMC не меняет правило толпы у ИИ (CLAUDE.md, «Изоляция стратегий»)."""
        import llm_rules
        before = (llm_rules.DECISION.FUNDING_AGAINST_CROWD, llm_rules.DECISION.FUNDING_MAX_BP)
        monkeypatch.setattr(params, 'FUNDING_MAX_BP', 5.0)
        monkeypatch.setattr(params, 'FUNDING_AGAINST_CROWD', not params.FUNDING_AGAINST_CROWD)
        assert (llm_rules.DECISION.FUNDING_AGAINST_CROWD, llm_rules.DECISION.FUNDING_MAX_BP) == before
