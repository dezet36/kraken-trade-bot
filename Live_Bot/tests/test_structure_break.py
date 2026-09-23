"""
Стоп ИИ — за сломом структуры, вход — по живую сторону от него.

ОТКУДА. Аудит 22.09.2026 (`docs/Аудит_ИИ_2026-09-22.md`): из 14 планов у
десяти вход стоял ПО ТУ СТОРОНУ уровня, за которым восходящая структура
становится нисходящей. Цена могла прийти к такому входу, только сломав
тренд, ради которого входили; держать стоп после этого было не за что, и он
вставал за ближний край имбаланса — в треть дневного размаха. Десять сделок
из одиннадцати закрылись по стопу, сумма −7.55R.

Здесь проверяется вся цепочка: общий слой считает уровень, разметка отдаёт
его модели как обычный уровень (иначе грамматика не даст его назвать), код
отвергает план, который с этим уровнем не согласован.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm_decide  # noqa: E402


# ── Общий слой: где ломается структура ──────────────────────────────────────

class TestTheSharedLayerFindsIt:
    def _ctx(self):
        from test_strategy_isolation_behaviour import market, frames_of
        from smc import signal as smc_signal
        df = market(2000)
        return df, smc_signal.build_context(frames_of(df.copy()), pair='TEST')

    def test_bullish_structure_breaks_at_the_last_higher_low(self):
        from smc import structure as st
        df, ctx = self._ctx()
        idx = len(df) - 1
        state = st.state_at(ctx.structure, idx)
        found = st.invalidation_level(ctx.structure, idx)
        assert found is not None
        assert found['direction'] == state['trend']
        assert found['label'] == ('HL' if state['trend'] == 'BULLISH' else 'LH')
        # Уровень — из подтверждённых свингов, не из будущего.
        assert found['index'] <= idx
        price = float(df['close'].iloc[idx])
        if state['trend'] == 'BULLISH':
            assert found['price'] < price, 'в восходящей структуре слом ниже цены'
        else:
            assert found['price'] > price

    def test_it_is_a_confirmed_swing_not_a_fresh_extreme(self):
        from smc import structure as st
        df, ctx = self._ctx()
        idx = len(df) - 1
        found = st.invalidation_level(ctx.structure, idx)
        point = next(p for p in ctx.structure['points'] if p['index'] == found['index'])
        assert point['confirmed_at'] <= idx

    def test_direction_can_be_asked_explicitly(self):
        from smc import structure as st
        df, ctx = self._ctx()
        idx = len(df) - 1
        up = st.invalidation_level(ctx.structure, idx, 'BULLISH')
        down = st.invalidation_level(ctx.structure, idx, 'BEARISH')
        assert up and down and up['price'] != down['price']
        assert up['label'] == 'HL' and down['label'] == 'LH'


# ── Разметка: уровень обязан попасть в таблицу ──────────────────────────────

class TestTheModelCanNameIt:
    def test_the_break_level_becomes_a_level_candidate(self):
        import llm_context
        market = {'structure_break': {'poi': {'tf': '1ч', 'price': 123.45,
                                              'label': 'HL', 'broken': False}}}
        extra = llm_context.extra_levels(market)
        found = [x for x in extra if 'слом структуры' in x['kind']]
        assert found, 'уровень слома не попал в кандидаты — модель не сможет его назвать'
        assert found[0]['price'] == 123.45
        assert found[0]['touches'] >= 4, 'вес мал: уровень вытеснят из таблицы'


# ── Ворота кода ─────────────────────────────────────────────────────────────

MARKET = {'structure_break': {'poi': {'tf': '1ч', 'price': 100.0, 'label': 'HL',
                                      'broken': False}}}


class TestThePlanMustAgreeWithTheStructure:

    def test_a_long_entry_below_the_break_is_refused(self):
        why = llm_decide.plan_against_structure('LONG', 99.0, 96.0, MARKET)
        assert 'ниже' in why and 'сломав тренд' in why

    def test_a_long_entry_above_the_break_with_a_stop_beyond_it_passes(self):
        assert llm_decide.plan_against_structure('LONG', 105.0, 99.4, MARKET) == ''

    def test_a_stop_that_does_not_reach_the_break_is_refused(self):
        why = llm_decide.plan_against_structure('LONG', 105.0, 101.0, MARKET)
        assert 'не доходит' in why

    def test_a_short_mirrors(self):
        assert llm_decide.plan_against_structure('SHORT', 95.0, 100.6, MARKET) == ''
        assert llm_decide.plan_against_structure('SHORT', 101.0, 105.0, MARKET) != ''
        assert llm_decide.plan_against_structure('SHORT', 95.0, 99.0, MARKET) != ''

    def test_without_the_level_nothing_is_claimed(self):
        assert llm_decide.plan_against_structure('LONG', 105.0, 99.0, {}) == ''
        assert llm_decide.plan_against_structure('LONG', 105.0, 99.0, None) == ''


class TestTheStopIsNotInsideALiveZone:
    """Дыра, найденная аудитом: ворота читали только часовые зоны."""

    def test_a_stop_inside_a_four_hour_imbalance_is_refused(self):
        market = {'htf_zones': [{'bottom': 95.0, 'top': 99.0, 'kind': 'FVG'}]}
        why = llm_decide.stop_inside_live_zone('LONG', 97.0, market)
        assert 'имбаланса 4ч' in why and '95' in why

    def test_a_stop_inside_an_hourly_imbalance_is_refused(self):
        market = {'fvgs': [{'bottom': 95.0, 'top': 99.0}]}
        assert llm_decide.stop_inside_live_zone('LONG', 97.0, market) != ''

    def test_a_stop_beyond_the_zone_passes(self):
        market = {'htf_zones': [{'bottom': 95.0, 'top': 99.0, 'kind': 'FVG'}]}
        assert llm_decide.stop_inside_live_zone('LONG', 94.4, market) == ''

    def test_a_short_stop_above_the_zone_passes(self):
        market = {'htf_zones': [{'bottom': 95.0, 'top': 99.0, 'kind': 'FVG'}]}
        assert llm_decide.stop_inside_live_zone('SHORT', 99.6, market) == ''
        assert llm_decide.stop_inside_live_zone('SHORT', 98.0, market) != ''


class TestTheTargetMustBeReachable:

    def test_a_target_nearer_than_the_entry_is_refused(self):
        # LTC 21.09: до цели 0.92%, до входа 4.13%.
        why = llm_decide.target_out_of_reach('LONG', 60.13, 63.294, 62.72)
        assert 'раньше, чем даст войти' in why

    def test_a_normal_plan_passes(self):
        assert llm_decide.target_out_of_reach('LONG', 100.0, 110.0, 101.0, 12.0) == ''

    def test_a_target_beyond_the_daily_range_is_refused(self):
        why = llm_decide.target_out_of_reach('LONG', 100.0, 130.0, 101.0, 7.0)
        assert 'за часы не достаётся' in why

    def test_without_the_daily_range_only_the_near_target_is_checked(self):
        assert llm_decide.target_out_of_reach('LONG', 100.0, 130.0, 101.0, None) == ''

    def test_entry_at_the_price_is_not_a_trap(self):
        """Вход по текущей цене — до него ноль, и сравнение плеч бессмысленно."""
        assert llm_decide.target_out_of_reach('LONG', 100.0, 101.0, 100.0, 12.0) == ''


class TestTheGatesAreRegistered:
    def test_new_gates_are_listed_as_code_gates(self):
        for gate in ('план против структуры', 'стоп внутри живой зоны', 'цель недостижима'):
            assert gate in llm_decide.CODE_GATES


class TestTheLevelMatchesThePlanSide:
    """
    Лонг умирает под последним HL, шорт — над последним LH. Сверять лонг с
    медвежьим уровнем (и наоборот) — значит отказывать не по делу.
    """

    MARKET = {'structure_break': {'poi': {'tf': '1ч', 'price': 120.0, 'label': 'LH',
                                          'direction': 'BEARISH', 'broken': True,
                                          'long': 100.0, 'short': 120.0}}}

    def test_a_long_is_measured_against_the_bullish_level(self):
        assert llm_decide.structure_break_price(self.MARKET, side='LONG') == 100.0
        # Вход выше HL, стоп за ним — план проходит, хотя структура медвежья.
        assert llm_decide.plan_against_structure('LONG', 105.0, 99.0, self.MARKET) == ''

    def test_a_short_is_measured_against_the_bearish_level(self):
        assert llm_decide.structure_break_price(self.MARKET, side='SHORT') == 120.0
        assert llm_decide.plan_against_structure('SHORT', 115.0, 121.0, self.MARKET) == ''
        assert llm_decide.plan_against_structure('SHORT', 125.0, 130.0, self.MARKET) != ''

    def test_without_a_side_the_current_trend_level_is_used(self):
        assert llm_decide.structure_break_price(self.MARKET) == 120.0

    def test_a_missing_side_level_falls_back(self):
        market = {'structure_break': {'poi': {'tf': '1ч', 'price': 120.0,
                                              'long': None, 'short': 120.0}}}
        assert llm_decide.structure_break_price(market, side='LONG') == 120.0
