"""SMC с нуля (30.09.2026) — данные.

Часовые свечи с потоком тейкеров, ОИ и фандингом из flow_cache*.
Периоды протокола (docs/SMC0_с_нуля_2026-09-30.md):
    dev   2022-01-01 … 2024-06-30   — поиск правил
    val   2024-07-01 … 2025-06-30   — выбор из финалистов
    hold  2025-07-01 … 2026-09-27   — один прогон
    y21   2020-11-01 … 2021-12-31   — один прогон (flow_cache_2021)
    other — 22 пары вне пула, 2022-01 … 2026-09, один прогон
"""
import os
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))

POOL = ['AAVEUSDT', 'ADAUSDT', 'ARBUSDT', 'AVAXUSDT', 'BNBUSDT', 'BTCUSDT',
        'COTIUSDT', 'DOGEUSDT', 'DOTUSDT', 'ETHUSDT', 'LINKUSDT', 'LTCUSDT',
        'NEARUSDT', 'SHIB1000USDT', 'SOLUSDT', 'SUIUSDT', 'UNIUSDT',
        'XLMUSDT', 'XRPUSDT', 'ZECUSDT']

PERIODS = {
    'dev': ('2022-01-01', '2024-07-01'),
    'val': ('2024-07-01', '2025-07-01'),
    'hold': ('2025-07-01', '2026-09-28'),
    'y21': ('2020-11-01', '2022-01-01'),
}
WARMUP_DAYS = 45


def _load_one(sym, folders):
    parts = []
    for f in folders:
        p = os.path.join(BASE, f, sym + '.pkl')
        if os.path.exists(p):
            parts.append(pd.read_pickle(p))
    if not parts:
        return None
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep='last')].sort_index()
    return df


def load(sym, period, folders=None):
    """Свечи пары за период + разогрев перед ним. Колонка 'live' — бары
    внутри периода (сигналы берутся только на них)."""
    if folders is None:
        folders = ['flow_cache_2021', 'flow_cache', 'flow_cache_new',
                   'flow_cache_extra']
    df = _load_one(sym, folders)
    if df is None:
        return None
    a, b = PERIODS[period] if isinstance(period, str) else period
    a = pd.Timestamp(a, tz='UTC')
    b = pd.Timestamp(b, tz='UTC')
    df = df[(df.index >= a - pd.Timedelta(days=WARMUP_DAYS)) & (df.index < b + pd.Timedelta(days=4))]
    if len(df) < 500:
        return None
    # ровная часовая сетка: дыры не заполняем ценой, а помечаем
    full = pd.date_range(df.index[0], df.index[-1], freq='1h')
    df = df.reindex(full)
    df['gap'] = df['c'].isna()
    for col in ('o', 'h', 'l', 'c'):
        df[col] = df[col].ffill()
    df['v'] = df['v'].fillna(0.0)
    df['tb'] = df['tb'].fillna(0.0)
    df['oi'] = df['oi'].ffill()
    df['funding'] = df['funding'].ffill().fillna(0.0)
    df['live'] = (df.index >= a) & (df.index < b)
    df['sig_ok'] = df['live']  # сигнал можно ставить только внутри периода
    return df


def resample(df, rule='4h'):
    """Старший таймфрейм из часовых свечей (бар подписан временем открытия)."""
    agg = {'o': 'first', 'h': 'max', 'l': 'min', 'c': 'last', 'v': 'sum', 'tb': 'sum',
           'oi': 'last', 'funding': 'first', 'gap': 'any', 'live': 'first', 'sig_ok': 'first',
           'fund_sum': 'sum'}
    df = df.copy()
    # сумма ставок в моменты расчёта (00/08/16 UTC) внутри бара
    df['fund_sum'] = np.where(df.index.hour % 8 == 0, df['funding'], 0.0)
    out = df.resample(rule, label='left', closed='left').agg(agg)
    out = out.dropna(subset=['c'])
    for col in ('gap', 'live', 'sig_ok'):
        out[col] = out[col].astype(bool)
    return out


def other_pairs():
    out = []
    for f in ('flow_cache_new', 'flow_cache_extra'):
        for n in sorted(os.listdir(os.path.join(BASE, f))):
            if n.endswith('.pkl'):
                s = n[:-4]
                if s not in POOL and s not in out:
                    out.append(s)
    return out
