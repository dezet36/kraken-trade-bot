"""
strategy_profile — единственное место, где решается «своё или общее» для
величин исполнения: срок заявки, кулдаун, предел издержек, смещение
лимита, предел удержания.

Зачем тест. Пока ответ лежал в трёх модулях, он расходился: у SMC в params
стоял срок 48 ч и кулдаун 12 ч, брокер брал 72 и 12 из config; у Боллинджера
кулдаун 2 ч — брокер брал 12; предел издержек 5% из config за трое суток
(19–21.09.2026) снял девять сетапов SMC и все сетапы уровней. Правило
проекта: общий параметр не имеет права запирать одну стратегию.
"""

import pytest

import strategy_profile as sp

# config берём через sp._config() в каждой проверке: другие наборы
# перезагружают его между проверками, и модуль, схваченный при импорте,
# был бы чужим.


ALL = ('FIBO', 'SMC', 'LEVELS', 'RSIBB', 'LLM')


class TestEveryValueComesFromTheStrategy:

    def test_expiry(self):
        from levels import params as lv
        from rsibb import params as rb
        from smc import params as smc
        assert sp.expiry_hours('LEVELS') == pytest.approx(lv.EXPIRY_HOURS)
        assert sp.expiry_hours('RSIBB') == pytest.approx(rb.EXPIRY_BARS * sp._bar_hours(rb.TIMEFRAME))
        assert sp.expiry_hours('SMC') == pytest.approx(smc.PENDING_ORDER_MAX_HOURS)
        assert sp.expiry_hours('LLM') == pytest.approx(sp._config().LLM_TRIGGER_TTL_H)
        assert sp.expiry_hours('FIBO') == pytest.approx(sp._config().PENDING_ORDER_MAX_HOURS)

    def test_cooldown(self):
        from levels import params as lv
        from rsibb import params as rb
        from smc import params as smc
        assert sp.cooldown_hours('LEVELS') == pytest.approx(lv.COOLDOWN_HOURS)
        assert sp.cooldown_hours('RSIBB') == pytest.approx(rb.COOLDOWN_HOURS)
        assert sp.cooldown_hours('SMC') == pytest.approx(smc.COOLDOWN_HOURS)
        assert sp.cooldown_hours('LLM') == pytest.approx(sp._config().LLM_COOLDOWN_HOURS)
        assert sp.cooldown_hours('FIBO') == pytest.approx(sp._config().COOLDOWN_HOURS)

    def test_cost_limit(self):
        from levels import params as lv
        from rsibb import params as rb
        from smc import params as smc
        assert sp.cost_limit_pct('LEVELS') == pytest.approx(lv.MAX_ENTRY_COST_SHARE_PCT)
        assert sp.cost_limit_pct('RSIBB') == pytest.approx(rb.MAX_ENTRY_COST_SHARE_PCT)
        assert sp.cost_limit_pct('SMC') == pytest.approx(smc.MAX_ENTRY_COST_SHARE_PCT)
        assert sp.cost_limit_pct('LLM') == pytest.approx(sp._config().LLM_MAX_ENTRY_COST_SHARE_PCT)
        assert sp.cost_limit_pct('FIBO') == pytest.approx(sp._config().MAX_ENTRY_COST_SHARE_PCT)

    def test_max_hold(self):
        from levels import params as lv
        from rsibb import params as rb
        from smc import params as smc
        assert sp.max_hold_hours('LEVELS') == pytest.approx(lv.MAX_HOLD_HOURS)
        assert sp.max_hold_hours('RSIBB') == pytest.approx(rb.MAX_HOLD_BARS * sp._bar_hours(rb.TIMEFRAME))
        assert sp.max_hold_hours('SMC') == pytest.approx(smc.MAX_POSITION_HOLD_HOURS)
        assert sp.max_hold_hours('LLM') == pytest.approx(sp._config().LLM_MAX_HOLD_HOURS)
        assert sp.max_hold_hours('FIBO') == pytest.approx(sp._config().MAX_POSITION_HOLD_HOURS)

    def test_llm_limit_has_no_offset_others_keep_the_measured_one(self, monkeypatch):
        monkeypatch.setattr(sp._config(), 'USE_LIMIT_ENTRY', True)
        monkeypatch.setattr(sp._config(), 'LIMIT_ENTRY_OFFSET_PCT', 0.001)
        assert sp.limit_offset_pct('LLM') == 0.0, 'вход ИИ — точный уровень из плана'
        for name in ('FIBO', 'SMC', 'LEVELS', 'RSIBB'):
            assert sp.limit_offset_pct(name) == pytest.approx(0.001)
        monkeypatch.setattr(sp._config(), 'USE_LIMIT_ENTRY', False)
        assert all(sp.limit_offset_pct(n) == 0.0 for n in ALL)


class TestACommonParameterCannotLockOneStrategy:
    """Правка общего числа в config трогает только Фибоначчи."""

    @pytest.mark.parametrize('key,fn', [
        ('PENDING_ORDER_MAX_HOURS', sp.expiry_hours),
        ('COOLDOWN_HOURS', sp.cooldown_hours),
        ('MAX_ENTRY_COST_SHARE_PCT', sp.cost_limit_pct),
        ('MAX_POSITION_HOLD_HOURS', sp.max_hold_hours),
    ])
    def test_it(self, monkeypatch, key, fn):
        before = {n: fn(n) for n in ALL}
        monkeypatch.setattr(sp._config(), key, 0.01)
        after = {n: fn(n) for n in ALL}
        assert after['FIBO'] == pytest.approx(0.01)
        for n in ('SMC', 'LEVELS', 'RSIBB', 'LLM'):
            assert after[n] == pytest.approx(before[n]), f'{n} читает общий {key}'

    def test_the_llm_floor_in_the_markup_and_the_broker_check_are_one_number(self):
        # Минимальный стоп в разметке выводится из того же предела, по
        # которому брокер потом проверяет план: на минимальном стопе доля
        # ровно равна пределу и проходит (<=), смещение лимита у ИИ — 0.
        import llm_context
        import risk_gate
        floor = llm_context.min_stop_pct()
        assert floor == pytest.approx(sp._config().ENTRY_COST_ROUND_TRIP / (sp.cost_limit_pct('LLM') / 100) * 100)
        pricey, _, _ = risk_gate.cost_too_high(100.0, floor, sp._config().ENTRY_COST_ROUND_TRIP, sp.cost_limit_pct('LLM'))
        assert pricey is False


class TestBrokenStrategyParamsFallBackToTheCommonValue:

    def test_missing_attribute(self, monkeypatch):
        from smc import params as smc
        monkeypatch.delattr(smc, 'COOLDOWN_HOURS')
        assert sp.cooldown_hours('SMC') == pytest.approx(sp._config().COOLDOWN_HOURS)

    def test_unknown_strategy_gets_the_common_values(self):
        d = sp.describe('WHATEVER')
        assert d['expiry_hours'] == pytest.approx(sp._config().PENDING_ORDER_MAX_HOURS)
        assert d['cost_limit_pct'] == pytest.approx(sp._config().MAX_ENTRY_COST_SHARE_PCT)
        assert d['cooldown_hours'] == pytest.approx(sp._config().COOLDOWN_HOURS)


class TestDescribeListsEverything:

    def test_keys(self):
        for name in ALL:
            d = sp.describe(name)
            assert set(d) == {'expiry_hours', 'cooldown_hours', 'cost_limit_pct',
                              'limit_offset_pct', 'max_hold_hours', 'min_stop_pct',
                              'min_stop_knob', 'drops_at_target'}
            flags = ('min_stop_knob', 'drops_at_target')
            assert all(isinstance(v, float) for k, v in d.items() if k not in flags)
            assert all(isinstance(d[k], bool) for k in flags)


class TestTheMinStopKnobIsHonest:
    """
    Ручку «минимальный стоп» читают только FIBO и SMC; уровни и Боллинджер
    живут своим params.MIN_STOP_PCT, ИИ — выводом из издержек. Панель обязана
    показывать число, которым стратегия живёт, а не мёртвое поле с 0.8%.
    """

    def test_who_reads_the_knob(self):
        assert set(sp.MIN_STOP_KNOB_READERS) == {'FIBO', 'SMC'}
        for name in ALL:
            assert sp.describe(name)['min_stop_knob'] is (name in sp.MIN_STOP_KNOB_READERS)

    def test_levels_and_rsibb_report_their_own_parameter(self):
        from levels import params as lv
        from rsibb import params as rb
        assert sp.min_stop_pct('LEVELS') == pytest.approx(lv.MIN_STOP_PCT)
        assert sp.min_stop_pct('RSIBB') == pytest.approx(rb.MIN_STOP_PCT)

    def test_llm_reports_the_cost_derived_floor(self):
        import llm_context
        assert sp.min_stop_pct('LLM') == pytest.approx(llm_context.min_stop_pct())

    def test_fibo_and_smc_report_the_operator_knob(self, monkeypatch):
        import settings_store
        monkeypatch.setattr(settings_store, 'min_stop_pct', lambda s: 0.0123)
        assert sp.min_stop_pct('FIBO') == pytest.approx(1.23)
        assert sp.min_stop_pct('SMC') == pytest.approx(1.23)
        # а чужие от ручки не зависят
        from levels import params as lv
        assert sp.min_stop_pct('LEVELS') == pytest.approx(lv.MIN_STOP_PCT)
