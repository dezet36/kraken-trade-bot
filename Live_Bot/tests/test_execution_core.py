"""
Ядро исполнения (execution/core.py) — реорганизация, этап 5.

Правила заявки и позиции вынесены из бумажного брокера; то, что брокер ведёт
себя как раньше, держит эталон tests/test_golden_broker.py. Здесь — сами
правила и их порядок (у каждого шага своя причина, см. paper_broker), и что
уровень снятия заявки ФИБО теперь объявляет сама стратегия — тот же, что
прежде брокер считал по её имени.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution import core  # noqa: E402

MAKER, TAKER = 0.0002, 0.00055


def an_order(direction='LONG', entry_type='LIMIT', **over):
    long_ = direction == 'LONG'
    order = {'direction': direction, 'entry_type': entry_type, 'limit_price': 100.0,
             'stop_loss': 95.0 if long_ else 105.0, 'targets': [110.0] if long_ else [90.0],
             'expires_ts': 10_000, 'invalidation': 95.0 if long_ else 105.0}
    order.update(over)
    return order


def a_position(direction='LONG', **over):
    long_ = direction == 'LONG'
    pos = {'direction': direction, 'entry_price': 100.0, 'size': 10.0, 'initial_size': 10.0,
           'stop_loss': 95.0 if long_ else 105.0, 'initial_stop': 95.0 if long_ else 105.0,
           'targets': [105.0, 110.0] if long_ else [95.0, 90.0], 'fractions': [0.5, 0.5],
           'be_level': None, 'breakeven_after_tp': True, 'breakeven_set': False,
           'tp_hit': 0, 'realized_pnl': 0.0, 'fees_paid': 0.0, 'opened_ts': 0,
           'mfe_price': 100.0, 'mae_price': 100.0, 'mfe_ts': 0, 'mae_ts': 0}
    pos.update(over)
    return pos


class TestPendingOrder:
    def test_expiry_comes_before_a_fill(self):
        """Свеча после срока заявку не застаёт, даже если цена прошла лимит."""
        assert core.simulate_pending(an_order(), 10_000, 101, 99) == ('expired', None)

    def test_going_beyond_comes_before_a_fill(self):
        order = an_order(cancel_beyond=103.0)
        assert core.simulate_pending(order, 1, 104, 99) == ('beyond', 103.0)

    def test_limit_fills_on_a_pullback_stop_entry_on_a_breakout(self):
        assert core.simulate_pending(an_order(), 1, 101, 99.5) == ('fill', 100.0)
        assert core.simulate_pending(an_order(entry_type='STOP'), 1, 100.5, 98) == ('fill', 100.0)
        assert core.simulate_pending(an_order(entry_type='STOP'), 1, 99.9, 98) == (None, None)

    def test_a_gap_through_the_level_fills_a_stop_entry_at_the_open(self):
        what, price = core.simulate_pending(an_order(entry_type='STOP'), 1, 103, 101, open_price=102)
        assert (what, price) == ('fill', 102)

    def test_a_fill_comes_before_invalidation_and_target(self):
        assert core.simulate_pending(an_order(), 1, 111, 94) == ('fill', 100.0)

    def test_broken_setup_and_target_without_us(self):
        # Ветка-страховка: уровень разрушения ближе к рынку, чем лимит (иначе
        # цена не дошла бы до него, не налив заявку — налив проверяется раньше).
        short = an_order('SHORT', invalidation=99.0)
        assert core.simulate_pending(short, 1, 99.5, 97) == ('broken', 99.0)
        gone = an_order('SHORT', limit_price=100.0, invalidation=None)
        assert core.simulate_pending(gone, 1, 99.5, 89) == ('target', None)
        assert core.simulate_pending(gone, 1, 99.5, 89, drops_at_target=False) == (None, None)

    def test_observation_does_not_change_decisions(self):
        order = an_order()
        core.simulate_pending(order, 1, 104, 101)
        assert order['min_gap_pct'] == pytest.approx(1.0)
        assert order['best_run_r'] == pytest.approx(0.8)

    def test_through_market_is_not_worse_than_the_limit(self):
        assert core.through_market_price(an_order(), 99.0, 0.001) == pytest.approx(99.099)
        assert core.through_market_price(an_order(), 99.95, 0.001) == 100.0


class TestOpenPosition:
    def events(self, pos, high, low, close=None, ts=60_000, max_hold=0):
        return list(core.simulate_position(pos, ts, high, low, close or (high + low) / 2, max_hold, MAKER))

    def test_stop_comes_before_a_target_inside_one_bar(self):
        assert self.events(a_position(), 111, 94) == [('close', (95.0, 'SL', True))]

    def test_partial_moves_the_stop_to_entry_and_the_last_target_closes(self):
        pos = a_position()
        assert self.events(pos, 106, 101) == [('target', (0, 105.0))]
        assert pos['stop_loss'] == 100.0 and pos['breakeven_set'] and pos['size'] == 5.0
        assert pos['realized_pnl'] == pytest.approx(25.0)
        assert self.events(pos, 111, 101) == [('close', (110.0, 'TP2', False))]

    def test_two_targets_in_one_bar_are_reported_one_by_one(self):
        """Генератор: обработчик видит позицию в момент каждой цели."""
        pos = a_position()
        seen = []
        for what, detail in core.simulate_position(pos, 60_000, 111, 101, 108, 0, MAKER):
            seen.append((what, pos['tp_hit'], pos['size']))
        assert seen == [('target', 1, 5.0), ('close', 1, 5.0)]

    def test_hold_limit_closes_at_the_bar_close(self):
        pos = a_position()
        assert self.events(pos, 101, 99, close=100.5, ts=3 * 3_600_000 + 1, max_hold=3) == [
            ('close', (100.5, 'TIME', True))]

    def test_breakeven_from_a_level_then_stop_in_the_same_bar(self):
        pos = a_position(be_level=103.0)
        events = self.events(pos, 103.5, 99.0)
        assert events[0] == ('breakeven', None)
        assert events[1][0] == 'close' and events[1][1][1] == 'BE'


class TestNumbers:
    def test_size_is_capped_and_risk_follows(self):
        assert core.position_size(10_000, 1.0, 2.0, 1_000_000) == (50.0, 100.0)
        assert core.position_size(10_000, 1.0, 2.0, 20) == (20, 40.0)

    def test_exit_fee_maker_on_targets_taker_otherwise(self):
        assert core.exit_fee_rate('TP2', MAKER, TAKER) == MAKER
        assert core.exit_fee_rate('SL', MAKER, TAKER) == TAKER
        assert core.exit_fee_rate('TIME', MAKER, TAKER) == TAKER

    def test_slippage_is_always_against_us(self):
        long_ = a_position()
        short = a_position('SHORT')
        assert core.close_numbers(long_, 95.0, 'SL', True, 0.001, MAKER, TAKER)[0] == pytest.approx(94.905)
        assert core.close_numbers(short, 105.0, 'SL', True, 0.001, MAKER, TAKER)[0] == pytest.approx(105.105)
        assert core.close_numbers(long_, 110.0, 'TP2', False, 0.001, MAKER, TAKER)[0] == 110.0

    def test_funding_is_paid_per_eight_hour_interval(self):
        pos = a_position(last_price=100.0, funding_ts=0)
        core.apply_funding(pos, 2 * core.FUNDING_INTERVAL_MS + 5, 0.0001)
        assert pos['funding_paid'] == pytest.approx(2 * 0.0001 * 1000.0)


class TestPendingInvalidationIsDeclaredByTheStrategy:
    def test_fibo_declares_the_level_the_broker_used_to_compute(self):
        """88.6%-уровень отката: прежде его считал paper_broker._invalidation по
        имени стратегии; теперь — сама ФИБО, тем же выражением."""
        config = __import__('importlib').import_module('infra.config')
        import pandas as pd
        strategy = __import__('importlib').import_module('strategies.fibo.strategy')
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'golden_market_1h.json')
        with open(path, encoding='utf-8') as fh:
            g = json.load(fh)['ETHUSDT']
        df = pd.DataFrame({'timestamp': pd.to_datetime(g['t'], unit='s', utc=True), 'open': g['o'],
                           'high': g['h'], 'low': g['l'], 'close': g['c'], 'volume': g['v']})
        checked = 0
        for i in range(120, len(df), 5):
            sig = strategy.analyze_market(df.iloc[i - 68:i].reset_index(drop=True), None, 'TEST')
            if not sig:
                continue
            setup = sig['setup']
            end, size = float(setup['end_price']), float(setup['size'])
            old = end - size * config.ZONE_B_TOP if setup['type'] == 'LONG' else end + size * config.ZONE_B_TOP
            assert float(sig['params']['pending_invalidation']) == old
            checked += 1
        assert checked > 50

    def test_broker_takes_the_declared_level_or_the_stop(self):
        paper_broker = __import__('importlib').import_module('execution.paper_broker')
        sig = {'setup': {'type': 'LONG', 'size': 5.0, 'end_price': 100.0},
               'params': {'stop_loss': 94.0, 'pending_invalidation': 95.57}}
        assert paper_broker.PaperBroker._invalidation('FIBO', sig, True) == 95.57
        del sig['params']['pending_invalidation']
        assert paper_broker.PaperBroker._invalidation('FIBO', sig, True) == 94.0
        assert paper_broker.PaperBroker._invalidation('SMC', sig, True) == 94.0
