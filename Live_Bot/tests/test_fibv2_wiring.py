"""
Фибоначчи V2 (FIBV2) и V2 · стоп 0.68 (FIBV2L) — схема владельца 28.10.2026.

Проверяется:
  - импульс общего слоя (analysis/impulse) в бою — окно закрытых часов и
    формирующаяся свеча — находит те же импульсы, что полный прогон по
    истории (на настоящих часах ETH и SOL); с лабораторией
    (D:/WORK_AI_Crypto/_fibo_lab_2026-10-28/imp_lab) модуль сверен 28.10 на
    SOL и COTI 2020–2026: 22 826 импульсов, ни одного расхождения;
  - сетап: только быстрый импульс, условия сторон (шорт — BTC за 30 дней рос,
    лонг — пробит максимум 7 суток), вход 0.5, стоп за началом (FIBV2) или за
    имбалансом / 0.68 (FIBV2L), четыре тейка по 25% и безубыток после первого;
  - сигнал без денег, узнаётся своей стратегией, один импульс — один сигнал;
  - исполнение — как в замере; стратегии известны реестру, брокеру, панели;
  - изоляция: правка параметров одной версии не меняет решений другой.
"""

import importlib
import json
import os
import sys

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from analysis import impulse  # noqa: E402

FLAT = 220


def drop_market():
    """Плоский рынок у 100 и резкое падение 100.5 → 91.0 за 5 свечей с тремя имбалансами."""
    rows = [(100.0, 100.3, 99.7, 100.1 if i % 2 else 99.9) for i in range(FLAT)]
    rows += [(100.0, 100.5, 99.0, 99.1),
             (99.1, 99.2, 97.0, 97.2),
             (97.2, 97.3, 95.0, 95.1),
             (95.1, 95.2, 93.0, 93.1),
             (93.1, 93.2, 91.0, 91.1)]
    return [list(x) for x in zip(*rows)]


def rise_market(prior_high=None):
    """Зеркало: резкий рост 99.5 → 109.0; prior_high — максимум за неделю до него."""
    rows = [(100.0, 100.3, 99.7, 100.1 if i % 2 else 99.9) for i in range(FLAT)]
    if prior_high:
        rows[FLAT - 100] = (100.0, prior_high, 99.7, 100.0)
    rows += [(100.0, 101.0, 99.5, 100.9),
             (100.9, 103.0, 100.8, 102.8),
             (102.8, 105.0, 102.7, 104.9),
             (104.9, 107.0, 104.8, 106.9),
             (106.9, 109.0, 106.8, 108.9)]
    return [list(x) for x in zip(*rows)]


def p2():
    return importlib.import_module('strategies.fibv2.params')


def p2l():
    return importlib.import_module('strategies.fibv2l.params')


def core2():
    return importlib.import_module('strategies.fibv2.core')


class TestImpulseLayer:
    def test_live_window_finds_the_same_impulses_as_the_full_history(self):
        """Бот видит 500 закрытых часов и формирующуюся свечу — импульсы те же, что в полном прогоне."""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'data', 'golden_market_1h.json'), encoding='utf-8') as fh:
            real = json.load(fh)
        W = 500
        for g in real.values():
            h, l = [float(x) for x in g['h']], [float(x) for x in g['l']]
            full, live = set(), set()
            for d in (1, -1):
                for s, e, k, _ in impulse.legs(h, l, d)[0]:
                    if k >= W + 50:
                        full.add((d, s, e, k))
            for k in range(W + 50, len(h)):
                wh, wl = h[k - W:k], l[k - W:k]
                for d in (1, -1):
                    _, st = impulse.legs(wh, wl, d)
                    if impulse.fixed_now(wh, wl, d, st, 0.382, h[k], l[k]):
                        live.add((d, k - W + st['s'], k - W + st['e'], k))
            assert live == full and len(full) > 100

    def test_small_pullbacks_do_not_end_the_impulse(self):
        o, h, l, c = drop_market()
        _, st = impulse.legs(h, l, -1)
        assert (st['s'], st['e']) == (FLAT, FLAT + 4)
        q = impulse.quality(o, h, l, c, impulse.atr_sma(h, l, c), -1, st['s'], st['e'])
        assert q['start'] == 100.5 and q['end'] == 91.0 and q['bars'] == 5 and len(q['gaps']) == 3
        assert q['dir_share'] == 1.0 and q['atr_per_bar'] > 1


class TestSetup:
    def evaluate(self, market, bar_high, bar_low, price, btc=0.05, rules=None):
        o, h, l, c = market
        return core2().evaluate(o, h, l, c, bar_high, bar_low, price, btc, rules or p2())

    def test_short_after_a_sharp_drop_on_a_rising_market(self):
        setup, reason = self.evaluate(drop_market(), 94.8, 91.2, 94.5)
        assert reason == 'ok' and setup['side'] == 'SHORT'
        assert setup['entry'] == pytest.approx(95.75)
        assert setup['stop'] == pytest.approx(91.0 + 9.5 * 1.01)            # за началом импульса
        assert setup['targets'] == pytest.approx([91.0, 91.0 - 0.18 * 9.5, 91.0 - 0.27 * 9.5, 91.0 - 0.618 * 9.5])

    def test_level_stop_goes_behind_the_nearest_deep_imbalance(self):
        setup, _ = self.evaluate(drop_market(), 94.8, 91.2, 94.5, rules=p2l())
        # имбалансы на глубинах 0.842, 0.632, 0.421: ближний глубже входа в окне 0.53–0.886 — 0.632
        assert setup['stop'] == pytest.approx(91.0 + 9.5 * (6.0 / 9.5 + 0.001 * 91.0 / 9.5))

    def test_no_short_when_btc_was_not_rising(self):
        setup, reason = self.evaluate(drop_market(), 94.8, 91.2, 94.5, btc=-0.01)
        assert setup is None and 'BTC за 30 дней не рос' in reason

    def test_nothing_before_the_first_38_correction(self):
        setup, reason = self.evaluate(drop_market(), 94.0, 91.2, 93.9)        # откат меньше 38.2%
        assert setup is None and reason == 'нет первой коррекции к 38.2%'

    def test_slow_move_is_not_an_impulse(self):
        o, h, l, c = drop_market()
        rows = list(zip(o, h, l, c))[:FLAT]
        price = 100.0
        for _ in range(12):                                                  # то же падение за 12 свечей
            rows.append((price, price + 0.05, price - 0.8, price - 0.75))
            price -= 0.75
        o, h, l, c = (list(x) for x in zip(*rows))
        low, top = min(l[FLAT:]), max(h[FLAT:])
        setup, reason = core2().evaluate(o, h, l, c, low + 0.5 * (top - low), low, low + 0.45 * (top - low), 0.05, p2())
        assert setup is None and ('свечей' in reason or 'медленно' in reason)

    def test_long_needs_a_breakout_of_the_week_high(self):
        setup, reason = self.evaluate(rise_market(), 109.0, 105.2, 105.5)
        assert reason == 'ok' and setup['side'] == 'LONG' and setup['entry'] == pytest.approx(104.25)
        setup, reason = self.evaluate(rise_market(prior_high=112.0), 109.0, 105.2, 105.5)
        assert setup is None and 'максимум 7 суток не пробит' in reason

    def test_tight_stop_is_a_refusal_not_a_move(self):
        rules = p2l()
        setup, reason = self.evaluate(drop_market(), 94.8, 91.2, 94.5, rules=rules)
        assert setup is not None
        old = rules.MIN_STOP_PCT
        try:
            rules.MIN_STOP_PCT = 5.0
            setup, reason = self.evaluate(drop_market(), 94.8, 91.2, 94.5, rules=rules)
            assert setup is None and 'теснее минимума' in reason
        finally:
            rules.MIN_STOP_PCT = old


@pytest.fixture()
def fibv2(monkeypatch):
    from strategies.fibv2 import adapter
    adapter._signaled.clear()
    adapter._last_reason.clear()
    o, h, l, c = drop_market()
    start = pd.Timestamp('2026-10-01 00:00')
    ts = pd.date_range(start, periods=len(c) + 1, freq='h')
    df = pd.DataFrame({'timestamp': ts, 'open': o + [91.1], 'high': h + [94.8], 'low': l + [91.2],
                       'close': c + [94.5], 'volume': 1.0})
    btc = pd.DataFrame({'timestamp': pd.date_range(start, periods=800, freq='h'), 'open': 1.0, 'high': 1.0,
                        'low': 1.0, 'close': [100.0] * 700 + [110.0] * 100, 'volume': 1.0})

    def fake(timeframe, limit=500, symbol=None, client=None, since=None):
        assert timeframe == '1h'
        return btc.tail(limit).reset_index(drop=True) if symbol == 'BTCUSDT' else df.tail(limit).reset_index(drop=True)

    monkeypatch.setattr(adapter, 'fetch_ohlcv', fake)
    return adapter, ts[-1] + pd.Timedelta(minutes=7)


class TestFunnelLabels:
    def test_only_size_refusals_read_as_small_impulse(self):
        """Воронка сводит строки со словом «импульс» к «импульс слишком мал» —
        остальные причины V2 этого слова не содержат."""
        from strategies import scan_report
        assert scan_report._label('нет первой коррекции к 38.2%') != 'импульс слишком мал'
        assert scan_report._label('ход 12 свечей — нужно 3–8') != 'импульс слишком мал'
        assert scan_report._label('импульс 2.1% меньше 3%') == 'импульс слишком мал'


class TestAdapter:
    def test_signal_is_the_owners_scheme_without_money(self, fibv2):
        adapter, now = fibv2
        signal = adapter.analyze_market('ETHUSDT', now=now)
        p = signal['params']
        assert p['tp_fractions'] == [0.25] * 4 and len(p['tp_targets']) == 4
        assert p['breakeven_after_tp'] is True and p['be_level'] is None
        assert p['cancel_beyond'] == 91.0 and p['max_hold_hours'] == 288.0
        assert signal['setup']['type'] == 'SHORT' and signal['fibv2']['stop_mode'] == 'start'
        from strategies import contract, registry
        assert contract.money_in(signal) == []
        assert registry.strategy_of(signal) == 'FIBV2'

    def test_one_impulse_one_signal(self, fibv2):
        adapter, now = fibv2
        assert adapter.analyze_market('ETHUSDT', now=now) is not None
        assert adapter.analyze_market('ETHUSDT', now=now) is None
        assert 'сигнал уже был' in adapter._last_reason['ETHUSDT']

    def test_stale_candles_are_not_traded(self, fibv2):
        adapter, now = fibv2
        assert adapter.analyze_market('ETHUSDT', now=now + pd.Timedelta(hours=2)) is None

    def test_execution_is_what_was_measured(self):
        sp = importlib.import_module('strategies.strategy_profile')
        for code in ('FIBV2', 'FIBV2L'):
            assert sp.expiry_hours(code) == 72.0 and sp.cooldown_hours(code) == 0.0
            assert sp.cost_limit_pct(code) == 100.0 and sp.max_hold_hours(code) == 288.0
            assert sp.drops_at_target(code) is True and sp.fills_through_market(code) is False
            assert sp.min_stop_pct(code) == 0.8


class TestIsolation:
    def test_editing_one_version_does_not_move_the_other(self, monkeypatch):
        o, h, l, c = drop_market()
        core_l = importlib.import_module('strategies.fibv2l.core')
        before = core_l.evaluate(o, h, l, c, 94.8, 91.2, 94.5, 0.05, p2l())[0]['stop']
        monkeypatch.setattr(p2(), 'MIN_ATR_PER_BAR', 50.0)
        monkeypatch.setattr(p2(), 'STOP_BEYOND_START', 2.0)
        assert core2().evaluate(o, h, l, c, 94.8, 91.2, 94.5, 0.05, p2())[0] is None
        assert core_l.evaluate(o, h, l, c, 94.8, 91.2, 94.5, 0.05, p2l())[0]['stop'] == before

    def test_own_env_prefixes(self):
        assert open(p2().__file__, encoding='utf-8').read().count("reader('FIBV2')") == 1
        assert open(p2l().__file__, encoding='utf-8').read().count("reader('FIBV2L')") == 1


class TestRegistration:
    def test_known_everywhere(self):
        from strategies import registry
        paper_broker = importlib.import_module('execution.paper_broker')
        settings_store = importlib.import_module('accounts.settings_store')
        config = importlib.import_module('infra.config')
        assert registry.get('FIBO').title == 'Фибоначчи V1'
        assert registry.get('FIBV2').title == 'Фибоначчи V2'
        for code in ('FIBV2', 'FIBV2L'):
            assert code in settings_store.STRATEGIES and code in paper_broker.STRATEGIES
            assert config.PAPER_START_BALANCES[code] == 10_000

    def test_guide_numbers(self):
        from test_strategy_guide import assert_quoted, guide_block
        for code, params in (('FIBV2', p2()), ('FIBV2L', p2l())):
            block = guide_block(code)
            assert_quoted(block, params.FIX_LEVEL * 100, 'фиксация импульса')
            assert_quoted(block, params.MIN_ATR_PER_BAR, 'ATR за свечу')
            assert_quoted(block, params.MIN_BARS, 'свечей от')
            assert_quoted(block, params.MAX_BARS, 'свечей до')
            assert_quoted(block, params.MIN_DIR_SHARE * 100, 'доля свечей по ходу')
            assert_quoted(block, params.MIN_IMPULSE_PCT, 'ход импульса')
            assert_quoted(block, params.ENTRY_LEVEL, 'вход')
            assert_quoted(block, params.PENDING_ORDER_MAX_HOURS, 'срок заявки')
            assert_quoted(block, params.MIN_STOP_PCT, 'минимальный стоп')
            assert_quoted(block, params.MAX_POSITION_HOLD_HOURS / 24, 'срок удержания')
        assert_quoted(guide_block('FIBV2'), p2().STOP_BEYOND_START, 'стоп за началом')
        assert_quoted(guide_block('FIBV2L'), p2l().STOP_LEVEL, 'стоп 0.68')
