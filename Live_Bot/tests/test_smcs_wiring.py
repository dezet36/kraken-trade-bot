"""
Подключение SMC-структуры 4ч (SMCS) к боту.

Что проверяется — уроки прежних подключений (test_rsibb_wiring):
- диспетчер обслуживает стратегию своей веткой, а не отдаёт кандидата чужой;
- сигнал несёт все поля общего договора;
- исполнение — то, что мерилось: вход ПО РЫНКУ сразу после закрытия бара
  слома (лимит за рынком, тейкер), стоп и цель — как в замере, срок 30 сут,
  без паузы после выхода, предел издержек 20% (в замере не отсёк бы ничего),
  восемь позиций в одну сторону (выбор владельца 02.10.2026);
- устаревший слом (бот стоял) входом не становится.
"""

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_paper_broker import broker_env  # noqa: E402,F401  (фикстура)
from _modules import forget, remember  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'smcs_golden.json')


@pytest.fixture()
def bot(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('infra.config', 'accounts.settings_store'):
        forget(module, None)
    import bot as module
    settings_store = __import__('importlib').import_module('accounts.settings_store')
    settings_store.SETTINGS_FILE = str(tmp_path / 'runtime_settings.json')
    settings_store._cache = None
    settings_store._mtime = None
    for name in ('bot', 'accounts.settings_store', 'strategies.smcs.adapter'):
        loaded = sys.modules.get(name)
        if loaded is not None and hasattr(loaded, 'settings'):
            loaded.settings = settings_store
    return module


def frame(pair, upto):
    """Свечи эталона по бар upto включительно + формирующаяся (копия)."""
    with open(GOLDEN, encoding='utf-8') as fh:
        g = json.load(fh)[pair]
    n = upto + 1
    df = pd.DataFrame({
        'timestamp': pd.to_datetime([t * 60 for t in g['t'][:n]], unit='s', utc=True),
        'open': g['o'][:n], 'high': g['h'][:n], 'low': g['l'][:n], 'close': g['c'][:n],
        'volume': [1.0] * n})
    forming = df.iloc[[-1]].copy()
    forming['timestamp'] = forming['timestamp'] + pd.Timedelta(hours=4)
    return pd.concat([df, forming], ignore_index=True), g


def a_bos(pair='ETHUSDT'):
    with open(GOLDEN, encoding='utf-8') as fh:
        g = json.load(fh)[pair]
    return next(e for e in g['events'] if e['kind'] == 'BOS' and e['bar'] > 100)


@pytest.fixture()
def adapter(bot, monkeypatch):
    from strategies.smcs import adapter as strategy_smcs
    state = {}

    def fake_fetch(timeframe, limit=500, symbol=None, client=None, since=None):
        assert timeframe == '4h'
        return state['df']
    monkeypatch.setattr(strategy_smcs, 'fetch_ohlcv', fake_fetch)
    return strategy_smcs, state


def bar_close(df):
    return df['timestamp'].iloc[-2] + pd.Timedelta(hours=4)


class TestAdapter:
    def test_signal_on_a_fresh_bos(self, adapter):
        smcs, state = adapter
        ev = a_bos()
        state['df'], g = frame('ETHUSDT', ev['bar'])
        now = bar_close(state['df']) + pd.Timedelta(minutes=5)
        sig = smcs.analyze_market('ETHUSDT', now=now)
        assert sig is not None, smcs._last_reason.get('ETHUSDT')
        for field in ('trading_pair', 'setup', 'params', 'trigger', 'zone', 'htf_trend',
                      'score', 'why', 'market_price', 'smcs'):
            assert field in sig, field
        for field in ('entry', 'stop_loss', 'take_profit_1', 'tp_targets', 'tp_fractions',
                      'sl_distance', 'rr', 'max_hold_hours'):
            assert field in sig['params'], field
        p = sig['params']
        assert p['stop_loss'] == pytest.approx(ev['stop'])
        assert p['tp_targets'] == [pytest.approx(ev['target'])]
        assert p['max_hold_hours'] == 720
        # Денег в сетапе нет; предел в одну сторону — с каким стратегия
        # измерена: его по умолчанию берёт счёт (strategy_profile).
        from strategies import contract
        strategy_profile = __import__('importlib').import_module('strategies.strategy_profile')
        assert contract.money_in(sig) == []
        assert strategy_profile.max_same_direction('SMCS') == 8
        assert p['be_level'] is None and p['breakeven_after_tp'] is False
        # лимит ЗА рынком: исполнится сразу, по рынку
        price = sig['market_price']
        if ev['dir'] == 1:
            assert sig['setup']['type'] == 'LONG' and p['entry'] > price
        else:
            assert sig['setup']['type'] == 'SHORT' and p['entry'] < price

    def test_stale_break_is_not_an_entry(self, adapter):
        smcs, state = adapter
        ev = a_bos()
        state['df'], _ = frame('ETHUSDT', ev['bar'])
        now = bar_close(state['df']) + pd.Timedelta(minutes=45)
        assert smcs.analyze_market('ETHUSDT', now=now) is None
        assert 'нового закрытого бара нет' in smcs._last_reason['ETHUSDT']

    def test_price_already_past_the_stop_refuses(self, adapter):
        smcs, state = adapter
        ev = a_bos()
        df, _ = frame('ETHUSDT', ev['bar'])
        beyond = ev['stop'] * (0.98 if ev['dir'] == 1 else 1.02)
        df.loc[df.index[-1], 'close'] = beyond
        state['df'] = df
        now = bar_close(df) + pd.Timedelta(minutes=5)
        assert smcs.analyze_market('ETHUSDT', now=now) is None


class TestDispatcher:
    def test_own_branch_keeps_the_scanner_signal(self, bot, adapter):
        smcs, state = adapter
        ev = a_bos()
        state['df'], _ = frame('ETHUSDT', ev['bar'])
        sig = smcs.analyze_market('ETHUSDT',
                                  now=bar_close(state['df']) + pd.Timedelta(minutes=1))
        built, _df = bot._build_signal({'pair': 'ETHUSDT', 'signal': sig, 'score': sig['score'],
                                        'rr': sig['params']['rr'], 'df_1h': None}, 'SMCS', 10_000)
        assert built is not None and built['strategy'] == 'SMCS'
        assert built['params']['stop_loss'] == pytest.approx(ev['stop'])
        assert built['scan']['kind'] == 'BOS'

    def test_candidate_without_signal_is_refused(self, bot):
        built, _ = bot._build_signal({'pair': 'ETHUSDT', 'score': 1.0, 'rr': 1.0,
                                      'df_1h': None}, 'SMCS', 10_000)
        assert built is None


class TestRegistration:
    def test_known_everywhere(self):
        paper_broker = __import__('importlib').import_module('execution.paper_broker')
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        config = __import__('importlib').import_module('infra.config')
        assert 'SMCS' in settings_store.STRATEGIES
        assert 'SMCS' in paper_broker.STRATEGIES
        assert config.PAPER_START_BALANCES['SMCS'] > 0

    def test_dashboard_knows_name_colour_and_guide(self):
        page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'control', 'dashboard.html')
        text = open(page, encoding='utf-8').read()
        assert "SMCS: 'SMC-структура 4ч'" in text
        assert text.count('--smcs:') >= 3
        assert '  SMCS: {' in text

    def test_execution_is_what_was_measured(self):
        sp = __import__('importlib').import_module('strategies.strategy_profile')
        assert sp.fills_through_market('SMCS') is True
        assert sp.drops_at_target('SMCS') is False
        assert sp.max_hold_hours('SMCS') == 720
        assert sp.cooldown_hours('SMCS') == 0
        assert sp.cost_limit_pct('SMCS') == 20
        assert sp.min_stop_pct('SMCS') == 0
        assert sp.expiry_hours('SMCS') == 1


class TestPaperFill:
    def test_entry_fills_at_once_at_the_market(self, broker_env, adapter):
        _b, client, pb, cfg = broker_env
        pb._now_ms = lambda: 1_700_000_000_000
        broker = pb.PaperBroker(client, strategies=('SMCS',))
        smcs, state = adapter
        ev = a_bos()
        state['df'], _ = frame('ETHUSDT', ev['bar'])
        sig = smcs.analyze_market('ETHUSDT',
                                  now=bar_close(state['df']) + pd.Timedelta(minutes=1))
        sig['strategy'] = 'SMCS'
        assert broker.open('SMCS', sig)
        assert not broker.pending('SMCS'), 'вход по рынку не ждёт свечи'
        pos = broker.positions('SMCS')['ETHUSDT']
        assert pos['entry_price'] == pytest.approx(sig['market_price'])
        assert 'по рынку' in pos['entry_note']
        assert pos['stop_loss'] == pytest.approx(ev['stop'])


class TestGuideNumbersMatchTheCode:
    """Цифры в описании на панели — те же, что в smcs/params. Расхождение
    значит, что человек читает про одну стратегию, а торгуется другая."""

    def test_numbers(self):
        from test_strategy_guide import assert_quoted, guide_block
        from strategies.smcs import params
        block = guide_block('SMCS')
        assert_quoted(block, params.SWING_K, 'размер свинга')
        assert_quoted(block, params.STOP_BUFFER_ATR, 'буфер стопа')
        assert_quoted(block, params.TARGET_R, 'цель в R')
        assert_quoted(block, params.MAX_POSITION_HOLD_HOURS / 24, 'срок в сутках')
        assert_quoted(block, params.MAX_SAME_DIRECTION, 'позиций в одну сторону')
        assert_quoted(block, params.SIGNAL_MAX_AGE_MIN, 'возраст слома')
