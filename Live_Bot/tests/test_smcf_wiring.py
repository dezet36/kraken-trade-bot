"""
SMCF — сетапы SMC с целью на ближайшем нетронутом имбалансе 4ч (27.10.2026,
research/luxsmc/PROTOCOL_GRAFT.md, вариант X5).

Проверяется:
  - имбаланс по правилу LuxAlgo (analysis/smc/imbalance.luxalgo_gaps): гэп,
    порог средней свечи, касание ближнего края — и по свечам 4ч, и по часам
    после последней закрытой 4ч;
  - цель — ближайший нетронутый имбаланс в сторону сделки не ближе 1R;
  - сигнал: вся позиция на цели, без денег, узнаётся как SMCF, а не SMC;
  - фильтр толпы — своя копия правила SMC;
  - изоляция: правка параметров SMC не меняет решений SMCF, и наоборот;
  - стратегия известна реестру, брокеру, настройкам, панели.
"""

import importlib
import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from analysis.smc import imbalance  # noqa: E402

H4 = pd.Timedelta(hours=4)


def candles_4h(rows, start='2026-09-01'):
    """rows — (open, high, low, close) по порядку, свечи 4ч с start."""
    o, h, l, c = (np.array(x, dtype=float) for x in zip(*rows))
    return pd.DataFrame({'timestamp': pd.date_range(start, periods=len(rows), freq='4h'),
                         'open': o, 'high': h, 'low': l, 'close': c, 'volume': 1.0})


def bearish_gap_market(after_high=91.0):
    """Плато 105, медвежий гэп [97; 104] на свече 32, дальше цена у 90.
    after_high — максимум свечей после гэпа (≥ 97 — край задет)."""
    rows = [(105, 105.5, 104.5, 105)] * 30
    rows += [(105, 106, 104, 105),            # i−2: low 104
             (104, 104.5, 97.5, 98),          # i−1: сильная медвежья, close < 104
             (96, 97, 94, 95)]                # i: high 97 < 104 — гэп
    rows += [(90, after_high, 89, 90)] * 20
    return candles_4h(rows)


class TestGaps:
    def test_bearish_gap_is_a_long_target_at_its_near_edge(self):
        gaps = imbalance.luxalgo_gaps(bearish_gap_market())
        # второй гэп — на следующей свече (high 91 < low 97.5), его край сразу задет
        assert [(g['target_side'], g['edge'], g['touched']) for g in gaps] == [(1, 97.0, False), (1, 91.0, True)]

    def test_touch_of_the_near_edge_ends_the_target(self):
        assert imbalance.luxalgo_gaps(bearish_gap_market(after_high=97.0))[0]['touched']

    def test_touch_inside_the_unclosed_4h_bar_is_seen_by_hours(self):
        df = bearish_gap_market()
        later = pd.DataFrame({'high': [92.0, 97.5], 'low': [89.0, 90.0]})
        assert imbalance.luxalgo_gaps(df, later=later)[0]['touched']

    def test_weak_middle_candle_is_not_a_gap(self):
        # тот же разрыв, но средняя свеча слабее порога: перед ней — сильные свечи
        rows = [(100, 110, 90, 108), (108, 112, 95, 98)] * 15
        rows += [(105, 106, 104, 105), (104, 104.5, 103.9, 103.95), (96, 97, 94, 95)]
        assert all(g['edge'] != 97.0 for g in imbalance.luxalgo_gaps(candles_4h(rows)))

    def test_matches_the_research_rule(self):
        """Правило то же, что в замере (research/luxsmc/graft.fvg_targets):
        гэпы и края на настоящих часах ETH и SOL, собранных в 4ч."""
        import json
        # Путь — здесь, а не константой: conftest уводит заглавные пути к боту во временную папку.
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'data', 'golden_market_1h.json'), encoding='utf-8') as fh:
            real = json.load(fh)
        for g in real.values():
            df = pd.DataFrame({'timestamp': pd.to_datetime(g['t'], unit='s'), 'open': g['o'],
                               'high': g['h'], 'low': g['l'], 'close': g['c']})
            h4 = (df.set_index('timestamp').resample('4h')
                  .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna().reset_index())
            o, h, l, c = (h4[k].to_numpy() for k in ('open', 'high', 'low', 'close'))
            delta = np.full(len(c), np.nan)
            delta[1:] = (c[:-1] - o[:-1]) / (o[:-1] * 100)
            cum = np.nancumsum(np.abs(np.nan_to_num(delta)))
            want = []
            for i in range(2, len(c)):
                thr = cum[i] / i * 2
                if l[i] > h[i - 2] and c[i - 1] > h[i - 2] and delta[i] > thr:
                    want.append((-1, l[i]))
                if h[i] < l[i - 2] and c[i - 1] < l[i - 2] and -delta[i] > thr:
                    want.append((1, h[i]))
            got = [(x['target_side'], x['edge']) for x in imbalance.luxalgo_gaps(h4)]
            assert got == want and len(got) > 5


@pytest.fixture()
def smcf(monkeypatch):
    from strategies.smcf import adapter
    adapter._history.clear()
    adapter._last_reason.clear()
    state = {'h4': bearish_gap_market(), 'rate': None, 'direction': 'BULLISH',
             'entry': 90.0, 'stop': 89.0}
    last_close = state['h4']['timestamp'].iloc[-1] + H4
    hours = pd.DataFrame({'timestamp': pd.date_range(last_close, periods=3, freq='h'),
                          'open': 90.0, 'high': 91.0, 'low': 89.5, 'close': 90.0, 'volume': 1.0})
    now_ms = int((last_close + pd.Timedelta(hours=3)).value // 10 ** 6)

    class Context:
        frames = {'poi': hours}

        def evaluate(self, at_index, decision=None):
            from strategies.smcf import params
            assert decision is params, 'SMCF решает своими правилами'
            return {'direction': state['direction'],
                    'params': {'entry': state['entry'], 'stop_loss': state['stop'], 'targets': [93.0, 95.0, 96.0],
                               'fractions': (0.25, 0.25, 0.5), 'rr': 5.2, 'sl_distance': 1.0},
                    'leg': {'start': {'price': 85.0, 'time': hours.timestamp[0]},
                            'end': {'price': 92.0, 'time': hours.timestamp[1]}, 'size': 7.0},
                    'poi': {'type': 'ORDER_BLOCK', 'top': 90.0, 'bottom': 89.3},
                    'confluence': 4.8, 'factors': {'ote_zone': True, 'killzone': False}}, None

    def fake_ohlcv(timeframe, limit=500, symbol=None, client=None, since=None):
        assert timeframe == '4h' and since is not None
        return state['h4']

    monkeypatch.setattr(adapter.structure, 'get', lambda pair, client=None: Context())
    monkeypatch.setattr(adapter, 'fetch_ohlcv', fake_ohlcv)
    monkeypatch.setattr(adapter, '_funding_rate', lambda pair, client=None: state['rate'])
    monkeypatch.setattr(adapter.settings, 'min_stop_pct', lambda name: 0.008)
    return adapter, state, now_ms


class TestAdapter:
    def test_whole_position_at_the_nearest_untouched_4h_gap(self, smcf):
        adapter, state, now_ms = smcf
        signal = adapter.analyze_market('BTCUSDT', now_ms=now_ms)
        p = signal['params']
        assert p['tp_targets'] == [97.0] and p['tp_fractions'] == [1.0]
        assert p['entry'] == 90.0 and p['stop_loss'] == 89.0 and p['rr'] == pytest.approx(7.0)
        assert signal['smcf']['fib_targets'] == [93.0, 95.0, 96.0] and 'smc' not in signal
        from strategies import contract, registry
        assert contract.money_in(signal) == []
        assert registry.strategy_of(signal) == 'SMCF'

    def test_gap_closer_than_1r_is_not_a_target(self, smcf):
        adapter, state, now_ms = smcf
        state['stop'] = 80.0                     # риск 10: гэп в 0.7R
        assert adapter.analyze_market('BTCUSDT', now_ms=now_ms) is None
        assert 'нет нетронутого имбаланса' in adapter._last_reason['BTCUSDT']

    def test_no_gap_on_the_side_of_the_trade(self, smcf):
        adapter, state, now_ms = smcf
        state.update(direction='BEARISH', entry=90.0, stop=91.0)
        assert adapter.analyze_market('BTCUSDT', now_ms=now_ms) is None

    def test_crowd_on_the_side_of_the_trade_is_a_refusal(self, smcf):
        adapter, state, now_ms = smcf
        state['rate'] = 0.0001                   # +1 б.п.: толпа в лонгах, лонг не берётся
        assert adapter.analyze_market('BTCUSDT', now_ms=now_ms) is None
        assert 'толпа за сделку' in adapter._last_reason['BTCUSDT']
        state['rate'] = -0.0001
        assert adapter.analyze_market('BTCUSDT', now_ms=now_ms) is not None

    def test_long_history_is_fetched_once_per_4h_bar(self, smcf, monkeypatch):
        adapter, state, now_ms = smcf
        calls = []
        fetch = adapter.fetch_ohlcv
        monkeypatch.setattr(adapter, 'fetch_ohlcv', lambda *a, **k: calls.append(k.get('since')) or fetch(*a, **k))
        adapter.analyze_market('BTCUSDT', now_ms=now_ms)
        adapter.analyze_market('BTCUSDT', now_ms=now_ms + 60_000)
        assert len(calls) == 1

    def test_execution_is_what_was_measured(self):
        sp = importlib.import_module('strategies.strategy_profile')
        assert sp.expiry_hours('SMCF') == 48.0 and sp.cooldown_hours('SMCF') == 12.0
        assert sp.cost_limit_pct('SMCF') == 10.0 and sp.max_hold_hours('SMCF') == 336.0
        assert sp.drops_at_target('SMCF') is False and sp.fills_through_market('SMCF') is False


class TestIsolation:
    """Правка SMC не меняет решений SMCF, правка SMCF — решений SMC."""

    @staticmethod
    def decisions(ctx, rules):
        out = []
        for i in range(800, len(ctx.frames['poi']), 7):
            setup, reason = ctx.evaluate(i, decision=rules)
            out.append(reason if setup is None else
                       (setup['direction'], round(setup['params']['entry'], 8), round(setup['params']['stop_loss'], 8)))
        return out

    def test_both_ways(self, monkeypatch):
        import test_strategy_isolation_behaviour as iso
        from analysis.smc.context import MarketContext
        smc_params = importlib.import_module('strategies.smc.params')
        smcf_params = importlib.import_module('strategies.smcf.params')
        ctx = MarketContext(iso.frames_of(iso.market()), pair='TEST')
        smc0, smcf0 = self.decisions(ctx, smc_params), self.decisions(ctx, smcf_params)
        assert any(not isinstance(x, str) for x in smcf0), 'на синтетике SMCF должна находить сетапы'
        monkeypatch.setattr(smc_params, 'MIN_CONFLUENCE_SCORE', 3.0)
        monkeypatch.setattr(smc_params, 'MIN_RR', 2.0)
        assert self.decisions(ctx, smcf_params) == smcf0
        assert self.decisions(ctx, smc_params) != smc0
        monkeypatch.undo()
        monkeypatch.setattr(smcf_params, 'MIN_CONFLUENCE_SCORE', 3.0)
        monkeypatch.setattr(smcf_params, 'MIN_RR', 2.0)
        assert self.decisions(ctx, smc_params) == smc0

    def test_every_rule_the_context_reads_is_in_own_params(self):
        import re
        bot = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(bot, 'analysis', 'smc', 'context.py'), encoding='utf-8').read()
        smcf_params = importlib.import_module('strategies.smcf.params')
        missing = sorted({m for m in re.findall(r'\bd\.([A-Z_0-9]+)', src) if not hasattr(smcf_params, m)})
        assert not missing, missing


class TestRegistration:
    def test_known_everywhere(self):
        from strategies import registry
        paper_broker = importlib.import_module('execution.paper_broker')
        settings_store = importlib.import_module('accounts.settings_store')
        config = importlib.import_module('infra.config')
        assert registry.get('SMCF').title == 'SMC → имбаланс 4ч'
        assert 'SMCF' in settings_store.STRATEGIES and 'SMCF' in paper_broker.STRATEGIES
        assert config.PAPER_START_BALANCES['SMCF'] == 10_000

    def test_guide_numbers(self):
        from test_strategy_guide import assert_quoted, guide_block
        from strategies.smcf import params
        block = guide_block('SMCF')
        assert_quoted(block, params.MIN_CONFLUENCE_SCORE, 'порог подтверждений')
        assert_quoted(block, params.MIN_RR, 'R:R по Фибоначчи')
        assert_quoted(block, params.TARGET_MIN_R, 'цель не ближе R')
        assert_quoted(block, params.PENDING_ORDER_MAX_HOURS, 'срок заявки')
        assert_quoted(block, params.MIN_SL_PCT * 100, 'минимальный стоп')
        assert_quoted(block, params.MAX_POSITION_HOLD_HOURS / 24, 'срок удержания в сутках')
