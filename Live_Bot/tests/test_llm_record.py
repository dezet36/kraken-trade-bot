"""
Что модель видела — числами и текстом — на каждый разбор: llm_record.

Разметка файлом, признаки строкой JSON, ключ (pair, at) общий с журналом.
Пропавший кусок снимка — None и флаг в missing_blocks, а не отсутствие строки.
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_record  # noqa: E402


def make_df(n=800):
    close = 100 + np.cumsum(np.random.RandomState(1).normal(0, 0.5, n))
    return pd.DataFrame({'timestamp': pd.to_datetime(np.arange(n) * 3_600_000, unit='ms'),
                         'open': close, 'high': close + 0.6, 'low': close - 0.6, 'close': close,
                         'volume': np.ones(n)})


MARKET = {
    'smc': {'bias': 'BULLISH', 'trend': 'BULLISH', 'side_of_range': 'PREMIUM', 'equilibrium': 99.0,
            'last_break': {'type': 'BOS', 'direction': 'BULLISH', 'bars_ago': 2}, 'sweep': {'side': 'BSL'}},
    'htf': {'htf': {'trend': 'BULLISH'}, 'bias': {'trend': 'BEARISH'}},
    'delta': {'fresh_min': 3, 'h1': {'share_pct': -22.9, 'rows': 60}, 'h4': {'share_pct': 1.4, 'rows': 240},
              'h24': {'share_pct': 7.1, 'rows': 1440}, 'divergence': 'цена растёт на продажах'},
    'oi_flow': {'change_pct': -1.94}, 'oi_week': 9.6, 'funding_trend': [0.00001, 0.00002, 0.00004],
    'book': {'imbalance': 0.93, 'spread_pct': 0.0001, 'walls_below': [1, 2], 'walls_above': [1]},
    'liq_fact': {'h24': {'long_size': 1e6, 'short_size': 2e6}, 'events': 800},
    'activity': {'last_x': 0.8, 'day_x': 0.9}, 'benchmark': {'btc_24h': 1.4, 'relative_24h': 2.1},
    'macro': {'usdt_d': 6.41, 'btc_d': 59.4, 'usdt_d_24h': 0.3, 'up_24h': 70, 'counted_24h': 77,
              'age_min': 2, 'stale': False},
    'day_profile': {'vwap_today': 100.5, 'poc_24h': 99.8}, 'profile': {'poc': 98.0, 'value_low': 96, 'value_high': 101},
    'sessions': {}, 'pois': [], 'fvgs': [], 'htf_zones': [], 'absorption': [], 'liquidations': {},
    'tape': [], 'delta_hours': [],
}
VERDICT = {'ok': True, 'gate': '', 'side': 'LONG', 'entry': 99.0, 'stop': 97.0, 'targets': [105.0],
           'p': 0.65, 'rr': 3.0, 'votes': 4, 'confluence': {'poi': True, 'vp': False, 'der': True, 'smc': True, 'flow': True},
           'trigger_when': 'retest', 'bias': 'up', 'stop_pct': 2.02, 'ids': {'entry': 'L4', 'stop': 'L7'},
           'levels': [{'id': 'L1', 'price': 106.0, 'kind': 'ближний край ликвидаций шортов'},
                      {'id': 'L4', 'price': 99.0, 'kind': 'верх имбаланса'},
                      {'id': 'L9', 'price': 95.0, 'kind': 'скопление минимумов'}],
           'thought': 'x' * 500, 'markup': 'РАЗМЕТКА ' * 50,
           'facts': {'atr_pct': 0.9, 'change_4h': 1.2, 'data_gap_bars': 0}, 'market_snapshot': MARKET}
STATS = {'seconds': 1000.5, 'prompt_tokens': 7800, 'cached_tokens': 3500, 'answer_tokens': 1600,
         'thought_tokens': 700, 'finish': 'stop', 'model': 'Qwen'}


@pytest.fixture(autouse=True)
def _own_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_record, 'MARKUP_DIR', str(tmp_path / 'llm_markup'))
    monkeypatch.setattr(llm_record, 'FEATURES_PATH', str(tmp_path / 'llm_features.jsonl'))


class TestFeaturesAreFlatAndComplete:
    def test_market_structure_flow_decision_and_quality(self):
        df = make_df()
        price = float(df['close'].iloc[-1])
        verdict = dict(VERDICT, entry=price * 0.99, stop=price * 0.97, targets=[price * 1.05],
                       levels=[{'id': 'L1', 'price': price * 1.05, 'kind': 'ближний край ликвидаций шортов'},
                               {'id': 'L4', 'price': price * 0.99, 'kind': 'верх имбаланса'},
                               {'id': 'L9', 'price': price * 0.95, 'kind': 'скопление минимумов'}])
        f = llm_record.features('LTCUSDT', df, VERDICT['facts'], MARKET, verdict, STATS, at='2026-09-21T12:59:42+00:00')
        assert f['pair'] == 'LTCUSDT' and f['at'] == '2026-09-21T12:59:42+00:00'
        assert f['trend_1h'] == 'BULLISH' and f['trend_4h'] == 'BULLISH' and f['trend_1d'] == 'BEARISH'
        assert f['delta_h1_share_pct'] == -22.9 and f['delta_h24_cover_min'] == 1440
        assert f['oi_change_pct_6b'] == -1.94 and f['funding_trend'] == 'up'
        assert f['macro_usdt_d'] == 6.41 and f['macro_breadth_pct'] == pytest.approx(90.9, abs=0.1)
        assert f['atr_pct'] == 0.9 and 'atr_percentile_30d' in f and 'range_pos_30d_pct' in f
        assert f['side'] == 'LONG' and f['p'] == 0.65 and f['cf_vp'] is False and f['trigger_when'] == 'retest'
        assert f['entry_dist_pct'] == pytest.approx(1.0, abs=0.01) and f['tp1_pct'] == pytest.approx(6.06, abs=0.01)
        assert f['pool_up_pct'] is not None and f['pool_down_pct'] is not None and f['levels_n'] == 3
        assert f['missing_blocks'] == [], 'полный снимок — ни одного пропуска'
        assert f['seconds'] == 1000.5 and f['thought_tokens'] == 700 and f['thought_chars'] == 500
        assert f['session'] in ('ASIA', 'LONDON', 'NY', 'LONDON_CLOSE', 'OFF')

    def test_missing_blocks_are_flagged_not_fatal(self):
        market = {k: (None if k in ('delta', 'book', 'macro') else v) for k, v in MARKET.items()}
        f = llm_record.features('X', make_df(), {}, market, {'ok': False, 'gate': 'модель пропустила'}, {})
        assert set(f['missing_blocks']) == {'delta', 'book', 'macro'}
        assert f['delta_h1_share_pct'] is None and f['decision'] == 'skip'

    def test_a_broken_snapshot_still_yields_a_row(self):
        f = llm_record.features('X', None, None, None, {}, None)
        assert f['pair'] == 'X' and f['decision'] == 'skip' and len(f['missing_blocks']) == len(llm_record.SNAPSHOT_BLOCKS)


class TestRecordWritesBothFiles:
    def test_markup_file_and_feature_line(self):
        row = llm_record.record('LTCUSDT', make_df(), dict(VERDICT), STATS, at='2026-09-21T12:59:42+00:00')
        assert row is not None
        path = os.path.join(llm_record.MARKUP_DIR, '2026-09-21T12-59-42_LTCUSDT.txt')
        assert os.path.exists(path) and open(path, encoding='utf-8').read().startswith('РАЗМЕТКА')
        lines = open(llm_record.FEATURES_PATH, encoding='utf-8').read().splitlines()
        assert len(lines) == 1
        back = json.loads(lines[0])
        assert back['markup_file'] == '2026-09-21T12-59-42_LTCUSDT.txt' and back['pair'] == 'LTCUSDT'
        assert back['at'] == '2026-09-21T12:59:42+00:00', 'ключ общий с журналом разборов'

    def test_a_refusal_without_markup_is_still_recorded(self):
        row = llm_record.record('X', make_df(), {'ok': False, 'gate': 'модель зависла'}, {}, at='2026-09-21T13:00:00+00:00')
        assert row['gate'] == 'модель зависла' and row['markup_file'] == ''
        assert not os.path.exists(llm_record.MARKUP_DIR)
