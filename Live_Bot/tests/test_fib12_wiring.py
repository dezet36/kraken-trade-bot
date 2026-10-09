"""
Подключение Фибо 12ч (FIB12) к боту.

Что проверяется — уроки прежних подключений (test_smcs_wiring):
- диспетчер обслуживает стратегию своей веткой, а не отдаёт кандидата чужой;
- сигнал несёт все поля общего договора;
- исполнение — то, что мерилось: лимит РОВНО на откате 0.382 (без сдвига),
  стоп за 0.786, цель 2.618, срок заявки 6 суток, позиция до 30 суток, без
  паузы, без предела в одну сторону (выбор владельца 07.10.2026);
- заявка снимается, если цена до налива ушла за конец импульса B, — и только
  у FIB12: у остальных стратегий уровня нет, их заявки этим не трогаются;
- сетап старше часа после закрытия бара входом не становится.
"""

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_paper_broker import broker_env  # noqa: E402,F401  (фикстура)

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'fib12_golden.json')


@pytest.fixture()
def bot(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('config', 'accounts.settings_store'):
        sys.modules.pop(module, None)
    import bot as module
    settings_store = __import__('importlib').import_module('accounts.settings_store')
    settings_store.SETTINGS_FILE = str(tmp_path / 'runtime_settings.json')
    settings_store._cache = None
    settings_store._mtime = None
    for name in ('bot', 'accounts.settings_store', 'strategies.fib12.adapter'):
        loaded = sys.modules.get(name)
        if loaded is not None and hasattr(loaded, 'settings'):
            loaded.settings = settings_store
    return module


def frame(pair, upto, price=None):
    """Свечи эталона по бар upto включительно + формирующаяся (её close —
    цена биржи на момент решения)."""
    with open(GOLDEN, encoding='utf-8') as fh:
        g = json.load(fh)[pair]
    n = upto + 1
    df = pd.DataFrame({
        'timestamp': pd.to_datetime([t * 60 for t in g['t'][:n]], unit='s', utc=True),
        'open': g['o'][:n], 'high': g['h'][:n], 'low': g['l'][:n], 'close': g['c'][:n],
        'volume': [1.0] * n})
    forming = df.iloc[[-1]].copy()
    forming['timestamp'] = forming['timestamp'] + pd.Timedelta(hours=12)
    if price is not None:
        forming['close'] = price
    return pd.concat([df, forming], ignore_index=True), g


def a_leg(pair='ETHUSDT', direction=None):
    with open(GOLDEN, encoding='utf-8') as fh:
        g = json.load(fh)[pair]
    confs = {}
    for leg in g['legs']:
        confs.setdefault(leg['conf'], []).append(leg)
    for conf, legs in sorted(confs.items()):
        valid = [x for x in legs if x['valid']]
        if valid and conf >= 499 and (direction is None or valid[0]['dir'] == direction):
            return valid[0]
    raise AssertionError('нет ноги в эталоне')


@pytest.fixture()
def adapter(bot, monkeypatch):
    from strategies.fib12 import adapter as strategy_fib12
    state = {}

    def fake_fetch(timeframe, limit=500, symbol=None, client=None, since=None):
        assert timeframe == '12h'
        return state['df']
    monkeypatch.setattr(strategy_fib12, 'fetch_ohlcv', fake_fetch)
    return strategy_fib12, state


def bar_close(df):
    return df['timestamp'].iloc[-2] + pd.Timedelta(hours=12)


class TestAdapter:
    def test_signal_on_a_fresh_leg(self, adapter):
        fib, state = adapter
        leg = a_leg()
        state['df'], _ = frame('ETHUSDT', leg['conf'])
        sig = fib.analyze_market('ETHUSDT', now=bar_close(state['df']) + pd.Timedelta(minutes=5))
        assert sig is not None, fib._last_reason.get('ETHUSDT')
        for field in ('trading_pair', 'setup', 'params', 'trigger', 'zone', 'htf_trend',
                      'score', 'why', 'market_price', 'fib12'):
            assert field in sig, field
        p = sig['params']
        for field in ('entry', 'stop_loss', 'take_profit_1', 'tp_targets', 'tp_fractions',
                      'sl_distance', 'rr', 'max_hold_hours', 'cancel_beyond'):
            assert field in p, field
        assert p['entry'] == pytest.approx(leg['entry'])
        assert p['stop_loss'] == pytest.approx(leg['stop'], rel=1e-6)
        assert p['tp_targets'] == [pytest.approx(leg['target'])]
        assert p['cancel_beyond'] == pytest.approx(leg['B'])
        assert p['max_hold_hours'] == 720
        # Денег в сетапе нет; предел в одну сторону — с каким стратегия
        # измерена: его по умолчанию берёт счёт (strategy_profile).
        from strategies import contract
        strategy_profile = __import__('importlib').import_module('strategies.strategy_profile')
        assert contract.money_in(sig) == []
        assert strategy_profile.max_same_direction('FIB12') == 0
        assert p['be_level'] is None and p['breakeven_after_tp'] is False
        assert sig['setup']['type'] == ('LONG' if leg['dir'] == 1 else 'SHORT')

    def test_stale_leg_is_not_an_entry(self, adapter):
        fib, state = adapter
        leg = a_leg()
        state['df'], _ = frame('ETHUSDT', leg['conf'])
        assert fib.analyze_market('ETHUSDT',
                                  now=bar_close(state['df']) + pd.Timedelta(minutes=90)) is None
        assert 'нового закрытого бара нет' in fib._last_reason['ETHUSDT']

    def test_price_already_beyond_b_refuses(self, adapter):
        fib, state = adapter
        leg = a_leg()
        beyond = leg['B'] * (1.01 if leg['dir'] == 1 else 0.99)
        state['df'], _ = frame('ETHUSDT', leg['conf'], price=beyond)
        assert fib.analyze_market('ETHUSDT',
                                  now=bar_close(state['df']) + pd.Timedelta(minutes=5)) is None
        assert 'концом импульса' in fib._last_reason['ETHUSDT']


class TestDispatcher:
    def test_own_branch_keeps_the_scanner_signal(self, bot, adapter):
        fib, state = adapter
        leg = a_leg()
        state['df'], _ = frame('ETHUSDT', leg['conf'])
        sig = fib.analyze_market('ETHUSDT', now=bar_close(state['df']) + pd.Timedelta(minutes=1))
        built, _df = bot._build_signal({'pair': 'ETHUSDT', 'signal': sig, 'score': sig['score'],
                                        'rr': sig['params']['rr'], 'df_1h': None}, 'FIB12', 10_000)
        assert built is not None and built['strategy'] == 'FIB12'
        assert built['params']['entry'] == pytest.approx(leg['entry'])
        assert built['scan']['poi_type'] == 'FIB'

    def test_candidate_without_signal_is_refused(self, bot):
        built, _ = bot._build_signal({'pair': 'ETHUSDT', 'score': 1.0, 'rr': 1.0,
                                      'df_1h': None}, 'FIB12', 10_000)
        assert built is None


class TestRegistration:
    def test_known_everywhere(self):
        import paper_broker
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        import config
        tg_format = __import__('importlib').import_module('control.tg_format')
        assert 'FIB12' in settings_store.STRATEGIES
        assert 'FIB12' in paper_broker.STRATEGIES
        assert config.PAPER_START_BALANCES['FIB12'] > 0
        assert tg_format.NAMES['FIB12'] == 'Фибо 12ч'

    def test_dashboard_knows_name_colour_and_guide(self):
        page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'control', 'dashboard.html')
        text = open(page, encoding='utf-8').read()
        assert "FIB12: 'Фибо 12ч'" in text
        assert text.count('--fib12:') >= 3
        assert '  FIB12: {' in text

    def test_execution_is_what_was_measured(self):
        sp = __import__('importlib').import_module('strategies.strategy_profile')
        assert sp.fills_through_market('FIB12') is True
        assert sp.drops_at_target('FIB12') is False
        assert sp.max_hold_hours('FIB12') == 720
        assert sp.cooldown_hours('FIB12') == 0
        assert sp.cost_limit_pct('FIB12') == 35
        assert sp.min_stop_pct('FIB12') == 0
        assert sp.expiry_hours('FIB12') == 144
        assert sp.limit_offset_pct('FIB12') == 0


class TestPaperBroker:
    def _placed(self, broker_env, adapter):
        _b, client, pb, cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker = pb.PaperBroker(client, strategies=('FIB12',))
        fib, state = adapter
        leg = a_leg()
        state['df'], _ = frame('ETHUSDT', leg['conf'])
        sig = fib.analyze_market('ETHUSDT', now=bar_close(state['df']) + pd.Timedelta(minutes=1))
        sig['strategy'] = 'FIB12'
        assert broker.open('FIB12', sig)
        return broker, leg

    def test_limit_waits_exactly_at_the_level(self, broker_env, adapter):
        broker, leg = self._placed(broker_env, adapter)
        order = broker.pending('FIB12')['ETHUSDT']
        assert order['limit_price'] == pytest.approx(leg['entry'])
        assert order['cancel_beyond'] == pytest.approx(leg['B'])

    def test_order_dropped_when_price_runs_beyond_b(self, broker_env, adapter):
        broker, leg = self._placed(broker_env, adapter)
        order = broker.pending('FIB12')['ETHUSDT']
        d = leg['dir']
        # свеча ушла за B и НЕ дошла до лимита
        hi, lo = (leg['B'] * 1.002, leg['entry'] * 1.01) if d == 1 else (leg['entry'] * 0.99, leg['B'] * 0.998)
        broker._process_pending('FIB12', 'ETHUSDT', order, order['placed_ts'] + 600_000, hi, lo)
        assert 'ETHUSDT' not in broker.pending('FIB12')
        assert 'ETHUSDT' not in broker.positions('FIB12')

    def test_order_fills_at_the_level_before_b(self, broker_env, adapter):
        broker, leg = self._placed(broker_env, adapter)
        order = broker.pending('FIB12')['ETHUSDT']
        d = leg['dir']
        e = leg['entry']
        hi, lo = (e * 1.002, e * 0.999) if d == 1 else (e * 1.001, e * 0.998)
        broker._process_pending('FIB12', 'ETHUSDT', order, order['placed_ts'] + 600_000, hi, lo)
        pos = broker.positions('FIB12')['ETHUSDT']
        assert pos['entry_price'] == pytest.approx(e)
        assert pos['stop_loss'] == pytest.approx(leg['stop'], rel=1e-6)


class TestOtherStrategiesUnaffected:
    def test_orders_without_the_level_ignore_it(self, broker_env):
        """Поле cancel_beyond объявляет только FIB12; у заявки без него ветка
        снятия не срабатывает, как бы далеко ни ушла цена по ходу."""
        _b, client, pb, cfg = broker_env
        broker = pb.PaperBroker(client, strategies=('SMCS',))
        order = {'direction': 'LONG', 'limit_price': 100.0, 'stop_loss': 95.0, 'targets': [200.0],
                 'expires_ts': 10 ** 15, 'placed_ts': 0, 'cancel_beyond': None}
        broker.state['pending']['SMCS']['X'] = order
        broker._process_pending('SMCS', 'X', order, 1, 150.0, 101.0)
        assert 'X' in broker.state['pending']['SMCS']


class TestGuideNumbersMatchTheCode:
    """Цифры в описании на панели — те же, что в fib12/params."""

    def test_numbers(self):
        from test_strategy_guide import assert_quoted, guide_block
        from strategies.fib12 import params
        block = guide_block('FIB12')
        assert_quoted(block, params.SWING_K, 'размер свинга')
        assert_quoted(block, params.STOP_BUFFER_ATR, 'буфер стопа')
        assert_quoted(block, params.MAX_POSITION_HOLD_HOURS / 24, 'срок в сутках')
        assert_quoted(block, params.PENDING_ORDER_MAX_HOURS / 24, 'срок заявки в сутках')
