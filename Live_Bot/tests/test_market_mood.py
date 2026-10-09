"""
Настроение рынка (market_mood): DVOL, премия Coinbase у BTC, доля спота — общий слой.

Формулы обязаны совпадать с research/ai_new_sources.enrich: там они проверялись
на истории (docs/ИИ_замечания_на_проверку.md, п. 82).
"""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
RESEARCH = os.path.join(os.path.dirname(HERE), 'research')

import config  # noqa: E402
from data import market_mood  # noqa: E402

H = 3_600_000
T0 = pd.Timestamp('2026-06-01', tz='UTC')


def series(n=800, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(T0, periods=n, freq='h')
    dvol = pd.Series(50 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=idx)
    spot = pd.Series(60000 * np.exp(np.cumsum(rng.normal(0, 0.003, n))), index=idx)
    cb = spot * (1 + rng.normal(0, 0.0005, n))
    spot_qv = pd.Series(rng.uniform(1e8, 3e8, n), index=idx)
    perp_qv = pd.Series(rng.uniform(5e8, 9e8, n), index=idx)
    return dict(dvol=dvol, cb_close=cb, spot_close=spot, spot_qv=spot_qv, perp_qv=perp_qv)


class TestSameAsResearch:
    def test_numbers_match_the_research_features(self, monkeypatch):
        if not os.path.exists(os.path.join(RESEARCH, 'ai_new_sources.py')):
            pytest.skip('нет research/')
        sys.path.insert(0, RESEARCH)
        import ai_new_sources as S
        s = series()
        frames = {'dvol': pd.DataFrame({'dvol': s['dvol']}),
                  'spot': pd.DataFrame({'spot_c': s['spot_close'], 'spot_qv': s['spot_qv'], 'spot_tbq': s['spot_qv'] / 2}),
                  'cb': pd.DataFrame({'cb_c': s['cb_close']})}
        monkeypatch.setattr(S, '_load', lambda kind, name: frames[kind])
        monkeypatch.setattr(S.enrich, '_done', None, raising=False)
        idx = s['dvol'].index
        df = pd.DataFrame({'qv': s['perp_qv']}, index=idx)
        f = pd.DataFrame({'taker_24h': 0.5}, index=idx)
        S.enrich({'BTCUSDT': (df, f)})
        for t in (idx[-1], idx[-200]):
            got = market_mood.compute(t, **s)
            assert got['dvol'] == pytest.approx(f.at[t, 'dvol'], abs=1e-3)
            assert got['dvol_chg_24h'] == pytest.approx(f.at[t, 'dvol_chg_24h'], abs=1e-3)
            assert got['dvol_pct_30d'] == pytest.approx(f.at[t, 'dvol_pct_30d'], abs=1e-3)
            assert got['btc_cb_prem_bp'] == pytest.approx(f.at[t, 'cb_prem_bp'], abs=1e-3)
            assert got['btc_spot_share_24h'] == pytest.approx(f.at[t, 'spot_share_24h'], abs=1e-3)


class TestLog:
    @pytest.fixture(autouse=True)
    def data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
        monkeypatch.setattr(market_mood, '_state', {'hour': None})

    def test_one_row_per_closed_hour_and_stale_is_a_dash(self):
        s = series()
        now = int((s['dvol'].index[-1] + pd.Timedelta(hours=1, minutes=3)).timestamp() * 1000)
        fetch = lambda t_ms: {k: v[v.index <= pd.Timestamp(t_ms, unit='ms', tz='UTC')] for k, v in s.items()}  # noqa: E731
        row = market_mood.collect_if_due(now, fetch=fetch)
        assert row and row['ts'] == now // H * H - H and row['dvol'] is not None
        assert market_mood.collect_if_due(now + 60_000, fetch=fetch) is None          # тот же час — один раз
        assert len(open(market_mood.path(), encoding='utf-8').readlines()) == 1
        assert market_mood.facts(now_ms=now)['btc_spot_share_24h'] == row['btc_spot_share_24h']
        assert market_mood.facts(now_ms=now + 3 * H) == {}                              # старше двух часов — прочерк

    def test_a_failed_source_leaves_its_fields_empty(self):
        s = series()
        now = int((s['dvol'].index[-1] + pd.Timedelta(hours=1, minutes=3)).timestamp() * 1000)
        only_dvol = lambda t_ms: {'dvol': s['dvol'][s['dvol'].index <= pd.Timestamp(t_ms, unit='ms', tz='UTC')]}  # noqa: E731
        row = market_mood.collect_if_due(now, fetch=only_dvol)
        assert row['dvol'] is not None and 'btc_cb_prem_bp' not in row and 'btc_spot_share_24h' not in row
        json.loads(open(market_mood.path(), encoding='utf-8').read())
