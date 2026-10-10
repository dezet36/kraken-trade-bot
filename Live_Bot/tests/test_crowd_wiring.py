"""
Подключение стратегии «Против толпы» (CROWD) к боту.

Что проверяется:
- торгуется ровно то, что мерилось: события (первая выплата от +5 б.п. после
  выплаты ниже) и ATR совпадают с исследовательским расчётом
  (research/crowdz/run.py) на настоящих выплатах Bybit;
- ставка приводится к 8 ч по шагу выплат;
- сигнал несёт поля общего договора, вход — по рынку (лимит за рынком), стоп
  1.5·ATR, цель 2R, срок 10 суток, без паузы и предела в одну сторону;
- выплата старше часа входом не становится;
- стратегия известна реестру, брокеру, настройкам и панели; цифры описания =
  параметры.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
RESEARCH = os.path.join(os.path.dirname(HERE), 'research')

from test_paper_broker import broker_env  # noqa: E402,F401  (фикстура)
from strategies.crowd import core  # noqa: E402

H8 = 8 * 3_600_000
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H8)


def rows(*bps, step=H8, start=T0):
    return [{'ts': start + i * step, 'value': bp / 1e4 * step / H8} for i, bp in enumerate(bps)]


class TestCore:
    def test_rate_is_brought_to_8h(self):
        four = rows(1.0, 3.0, step=H8 // 2)          # 3 б.п. за 4 ч = 6 б.п. за 8 ч
        pts = core.normalized(four)
        assert pts[-1][1] == pytest.approx(3.0)       # значение в журнале — уже за выплату
        raw = [{'ts': T0, 'value': 1e-4}, {'ts': T0 + H8 // 2, 'value': 3e-4}]
        assert core.normalized(raw)[-1][1] == pytest.approx(6.0)

    def test_first_payment_above_the_threshold_is_the_event(self):
        r = rows(1.0, 1.0, 6.0)
        ev, why = core.event(r, r[-1]['ts'] + 5 * 60_000)
        assert why is None and ev['bp'] == pytest.approx(6.0) and ev['prev_bp'] == pytest.approx(1.0)

    def test_not_an_event(self):
        assert core.event(rows(1.0, 4.9), T0 + H8 + 60_000)[0] is None
        r = rows(6.0, 7.0)
        ev, why = core.event(r, r[-1]['ts'] + 60_000)
        assert ev is None and 'не первую' in why
        r = rows(1.0, 6.0)
        ev, why = core.event(r, r[-1]['ts'] + 61 * 60_000)
        assert ev is None and 'мин назад' in why

    def test_setup_geometry(self):
        st, why = core.setup(100.0, 2.0)
        assert why is None and st['stop'] == pytest.approx(103.0) and st['target'] == pytest.approx(94.0)
        assert core.setup(100.0, 0.2)[0] is None              # стоп 0.3% < 0.5%


class TestMatchesTheResearch:
    """События и ATR — как в research/crowdz (замер), на настоящих данных."""

    @pytest.fixture()
    def research(self):
        if not os.path.isdir(os.path.join(RESEARCH, 'crowdz')):
            pytest.skip('нет research/')
        pytest.importorskip('numba')
        sys.path.insert(0, RESEARCH)
        from crowdz import run as R
        from smcz import data as D
        if not os.path.exists(os.path.join(D.CACHE, 'funding_bybit', 'ETHUSDT.npz')):
            pytest.skip('нет кэша фандинга')
        return R

    def test_events_match(self, research):
        R = research
        t, f, p5, p95 = R.funding('ETHUSDT')
        want = set(R.events(t, f, p5, p95, R.DEFS['s5'][1], 1).tolist())
        z = np.load(os.path.join(R.D.CACHE, 'funding_bybit', 'ETHUSDT.npz'))
        order = np.argsort(z['t'])
        jr = [{'ts': int(tt) * 60_000, 'value': float(v)} for tt, v in zip(z['t'][order], z['rate'][order])]
        got = set()
        for k in range(1, len(jr)):
            ev, _ = core.event(jr[max(0, k - 2):k + 1], jr[k]['ts'] + 60_000)
            if ev is not None:
                got.add(k)
        assert got == want - {0}

    def test_atr_matches(self, research):
        from smcz import structure as S
        rng = np.random.default_rng(3)
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))
        h, l = c * 1.01, c * 0.99
        assert core.atr(h.tolist(), l.tolist(), c.tolist(), 14)[-1] == pytest.approx(S.atr(h, l, c, 14)[-1])


@pytest.fixture()
def adapter(monkeypatch):
    from strategies.crowd import adapter as crowd
    from analysis import market
    state = {'rows': rows(1.0, 1.0, 8.0), 'price': 50.0}

    def fake_series(source, pair, limit=None, upto=None):
        assert source == 'funding'
        return state['rows'][-limit:] if limit else state['rows']

    def fake_ohlcv(timeframe, limit=500, symbol=None, client=None, since=None):
        assert timeframe == '4h'
        n = 60
        c = np.full(n, state['price'])
        return pd.DataFrame({'timestamp': pd.date_range('2026-10-01', periods=n, freq='4h', tz='UTC'),
                             'open': c, 'high': c * 1.01, 'low': c * 0.99, 'close': c, 'volume': 1.0})
    monkeypatch.setattr(market, 'series', fake_series)
    monkeypatch.setattr(market, 'settled_funding', lambda *a, **k: None)
    monkeypatch.setattr(crowd, 'fetch_ohlcv', fake_ohlcv)
    return crowd, state


class TestAdapter:
    def test_signal(self, adapter):
        crowd, state = adapter
        now = state['rows'][-1]['ts'] + 5 * 60_000
        sig = crowd.analyze_market('ETHUSDT', now_ms=now)
        assert sig is not None, crowd._last_reason.get('ETHUSDT')
        for field in ('trading_pair', 'setup', 'params', 'trigger', 'zone', 'htf_trend', 'score', 'why',
                      'market_price', 'crowd'):
            assert field in sig, field
        p = sig['params']
        assert sig['setup']['type'] == 'SHORT'
        assert p['entry'] < sig['market_price'], 'лимит за рынком — исполнится сразу'
        risk = p['stop_loss'] - sig['market_price']
        assert risk == pytest.approx(1.5 * sig['crowd']['atr'])
        assert sig['market_price'] - p['take_profit_1'] == pytest.approx(2 * risk)
        assert p['max_hold_hours'] == 240 and p['be_level'] is None
        from strategies import contract
        assert contract.money_in(sig) == []

    def test_stale_payment_is_not_an_entry(self, adapter):
        crowd, state = adapter
        assert crowd.analyze_market('ETHUSDT', now_ms=state['rows'][-1]['ts'] + 90 * 60_000) is None

    def test_execution_is_what_was_measured(self):
        sp = __import__('importlib').import_module('strategies.strategy_profile')
        assert sp.fills_through_market('CROWD') is True
        assert sp.max_hold_hours('CROWD') == 240
        assert sp.cooldown_hours('CROWD') == 0
        assert sp.expiry_hours('CROWD') == 1
        assert sp.max_same_direction('CROWD') == 0

    def test_entry_fills_at_once_at_the_market(self, broker_env, adapter):
        _b, client, pb, cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker = pb.PaperBroker(client, strategies=('CROWD',))
        crowd, state = adapter
        sig = crowd.analyze_market('ETHUSDT', now_ms=state['rows'][-1]['ts'] + 60_000)
        sig['strategy'] = 'CROWD'
        assert broker.open('CROWD', sig)
        assert not broker.pending('CROWD'), 'вход по рынку не ждёт свечи'
        assert broker.positions('CROWD')['ETHUSDT']['direction'] == 'SHORT'


class TestRegistration:
    def test_known_everywhere(self):
        from strategies import registry
        paper_broker = __import__('importlib').import_module('execution.paper_broker')
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        config = __import__('importlib').import_module('infra.config')
        assert registry.get('CROWD').title == 'Против толпы'
        assert 'CROWD' in settings_store.STRATEGIES and 'CROWD' in paper_broker.STRATEGIES
        assert config.PAPER_START_BALANCES['CROWD'] == 10_000

    def test_guide_numbers(self):
        from test_strategy_guide import assert_quoted, guide_block
        from strategies.crowd import params
        block = guide_block('CROWD')
        assert_quoted(block, params.THRESHOLD_BP, 'порог фандинга')
        assert_quoted(block, params.STOP_ATR, 'стоп в ATR')
        assert_quoted(block, params.TARGET_R, 'цель в R')
        assert_quoted(block, params.MAX_POSITION_HOLD_HOURS / 24, 'срок в сутках')
        assert_quoted(block, params.SIGNAL_MAX_AGE_MIN, 'возраст выплаты')
        assert_quoted(block, params.MIN_STOP_PCT, 'минимальный стоп')
