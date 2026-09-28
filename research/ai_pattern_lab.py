"""
Стенд закономерностей для ИИ (27.09.2026): признаки по часу и правила вида
«условия на признаки → сторона, срок, стоп», исполненные как бот.

ДАННЫЕ — research/flow_cache/<пара>.pkl (ai_flow_fetch.py): 20 пар бота,
2022-01…2026-09, час. Признаки считаются на ЗАКРЫТИИ часа i только по данным
до него; вход — открытие часа i+1 по рынку.

ИСПОЛНЕНИЕ. Вход по рынку (тейкер 0.055% + проскальзывание 0.03%), выход по
стопу (стоп — доля среднего суточного размаха за 14 дней) или по сроку на
закрытии часа (тейкер + проскальзывание). Одна позиция правила на пару. Исход —
в процентах за вычетом издержек и в R (к расстоянию до стопа).

ПЕРИОДЫ — заданы до замера и не меняются:
    train  2022-01-01 … 2024-06-30   отбор; только эти числа видит модель
    valid  2024-07-01 … 2025-04-30   подтверждение
    test   2025-05-01 … 2026-09-27   итоговая проверка, один раз
ПРИЁМКА ПРАВИЛА: train — средний R > 0 при t ≥ 2 и не реже 1 сделки в неделю
на все пары; valid — средний R > 0. Итог набора — на test, один раз.

    from ai_pattern_lab import load_all, evaluate
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, os.getenv('FLOW_CACHE', 'flow_cache'))

FEE = 0.00055
SLIP = 0.0003
SPLITS = {
    'train': ('2022-01-01', '2024-07-01'),
    'valid': ('2024-07-01', '2025-05-01'),
    'test': ('2025-05-01', '2026-09-28'),
}

# Описание признаков — и для людей, и для промта модели.
FEATURES = {
    'ret_1h': 'изменение цены за 1 час, %',
    'ret_4h': 'изменение цены за 4 часа, %',
    'ret_24h': 'изменение цены за 24 часа, %',
    'ret_72h': 'изменение цены за 72 часа, %',
    'ret_7d': 'изменение цены за 7 дней, %',
    'ret_30d': 'изменение цены за 30 дней, % (режим монеты)',
    'range_pos_7d': 'положение цены в диапазоне 7 дней: 0 — у минимума, 1 — у максимума',
    'atr_d': 'средний суточный размах за 14 дней, % цены',
    'vol_z': 'оборот за 24 ч к медиане суточного оборота за 30 дней (1 = обычно)',
    'taker_1h': 'доля покупок по рынку в объёме часа (0.5 — поровну)',
    'taker_4h': 'доля покупок по рынку за 4 часа',
    'taker_24h': 'доля покупок по рынку за 24 часа',
    'taker_24h_dev': 'доля покупок за 24 ч минус её среднее за 30 дней',
    'size_z': 'средний размер сделки за 24 ч к медиане за 30 дней (крупные участники)',
    'buy_ratio': 'доля счетов в лонге на Bybit (толпа), 0..1',
    'buy_ratio_chg_24h': 'изменение доли счетов в лонге за 24 ч, пункты',
    'buy_ratio_pct_30d': 'доля счетов в лонге: перцентиль за 30 дней (0..1)',
    'oi_chg_4h': 'изменение открытого интереса за 4 ч, %',
    'oi_chg_24h': 'изменение открытого интереса за 24 ч, %',
    'oi_chg_72h': 'изменение открытого интереса за 72 ч, %',
    'funding_bp': 'последняя ставка фандинга, б.п. за 8 ч (плюс — лонги платят)',
    'funding_pct_30d': 'ставка фандинга: перцентиль за 30 дней (0..1)',
    'btc_ret_4h': 'BTC за 4 часа, %',
    'btc_ret_24h': 'BTC за 24 часа, %',
    'btc_ret_7d': 'BTC за 7 дней, %',
    'btc_ret_30d': 'BTC за 30 дней, % (режим рынка)',
    'rel_24h': 'ход пары за 24 ч минус ход BTC, %',
    'hour_utc': 'час UTC закрытой свечи (0..23)',
    'weekday': 'день недели (0 — понедельник)',
}


def features(df, btc=None):
    c = df['c']
    out = pd.DataFrame(index=df.index)
    for h, name in ((1, 'ret_1h'), (4, 'ret_4h'), (24, 'ret_24h'), (72, 'ret_72h'), (168, 'ret_7d'),
                    (720, 'ret_30d')):
        out[name] = (c / c.shift(h) - 1) * 100
    hi7, lo7 = df['h'].rolling(168).max(), df['l'].rolling(168).min()
    out['range_pos_7d'] = (c - lo7) / (hi7 - lo7)
    day_range = (df['h'].rolling(24).max() - df['l'].rolling(24).min()) / c * 100
    out['atr_d'] = day_range.rolling(24 * 14).mean()
    qv24 = df['qv'].rolling(24).sum()
    out['vol_z'] = qv24 / qv24.rolling(24 * 30).median()
    out['taker_1h'] = df['tb'] / df['v']
    for h, name in ((4, 'taker_4h'), (24, 'taker_24h')):
        out[name] = df['tb'].rolling(h).sum() / df['v'].rolling(h).sum()
    out['taker_24h_dev'] = out['taker_24h'] - out['taker_24h'].rolling(24 * 30).mean()
    size = df['qv'].rolling(24).sum() / df['trades'].rolling(24).sum()
    out['size_z'] = size / size.rolling(24 * 30).median()
    br = df['buy_ratio'].ffill(limit=3)
    out['buy_ratio'] = br
    out['buy_ratio_chg_24h'] = (br - br.shift(24)) * 100
    out['buy_ratio_pct_30d'] = br.rolling(24 * 30, min_periods=24 * 20).rank(pct=True)
    oi = df['oi'].ffill(limit=3)
    for h, name in ((4, 'oi_chg_4h'), (24, 'oi_chg_24h'), (72, 'oi_chg_72h')):
        out[name] = (oi / oi.shift(h) - 1) * 100
    fr = df['funding'] * 1e4
    out['funding_bp'] = fr
    out['funding_pct_30d'] = fr.rolling(24 * 30, min_periods=24 * 20).rank(pct=True)
    if btc is not None:
        bc = btc['c'].reindex(df.index)
        for h, name in ((4, 'btc_ret_4h'), (24, 'btc_ret_24h'), (168, 'btc_ret_7d'), (720, 'btc_ret_30d')):
            out[name] = (bc / bc.shift(h) - 1) * 100
        out['rel_24h'] = out['ret_24h'] - out['btc_ret_24h']
    # Метка часа — открытие свечи; решение — на её закрытии.
    close_time = df.index + pd.Timedelta(hours=1)
    out['hour_utc'] = close_time.hour
    out['weekday'] = close_time.weekday
    return out


_DATA = {}


def load_all():
    if _DATA:
        return _DATA
    raw = {f[:-4]: pd.read_pickle(os.path.join(CACHE, f)) for f in sorted(os.listdir(CACHE)) if f.endswith('.pkl')}
    btc = raw.get('BTCUSDT')
    for pair, df in raw.items():
        df = df[df['v'] > 0]
        _DATA[pair] = (df, features(df, btc))
    return _DATA


OPS = {'<': np.less, '<=': np.less_equal, '>': np.greater, '>=': np.greater_equal}


def signal_mask(feat, conditions):
    m = np.ones(len(feat), bool)
    for name, op, value in conditions:
        col = feat[name].to_numpy(float)
        m &= OPS[op](col, float(value)) & ~np.isnan(col)
    return m


def simulate(df, mask, side, hold, stop_mult, atr_d):
    """Сделки правила на одной паре: вход на открытии следующего часа, стоп или срок."""
    o, h, l, c = (df[k].to_numpy(float) for k in ('o', 'h', 'l', 'c'))
    idx = np.flatnonzero(mask)
    trades, busy_until = [], -1
    n = len(df)
    sgn = 1.0 if side == 'long' else -1.0
    for i in idx:
        if i <= busy_until or i + 1 >= n or np.isnan(atr_d[i]):
            continue
        entry = o[i + 1] * (1 + sgn * SLIP)
        dist = stop_mult * atr_d[i] / 100.0
        stop = entry * (1 - sgn * dist)
        last = min(i + hold, n - 1)
        exit_px, j_exit = None, last
        for j in range(i + 1, last + 1):
            if (sgn > 0 and l[j] <= stop) or (sgn < 0 and h[j] >= stop):
                exit_px = stop * (1 - sgn * SLIP)
                j_exit = j
                break
        if exit_px is None:
            exit_px = c[last] * (1 - sgn * SLIP)
        gross = sgn * (exit_px / entry - 1)
        net = gross - 2 * FEE
        trades.append((df.index[i], net * 100, net / dist, j_exit - i))
        busy_until = j_exit
    return trades


def evaluate(rule, data=None, splits=('train', 'valid', 'test')):
    """Правило -> сводка по периодам: сделки, в неделю, средний % и R, t, доля плюсовых."""
    data = data or load_all()
    rows = []
    for pair, (df, feat) in data.items():
        if rule.get('pairs') and pair not in rule['pairs']:
            continue
        mask = signal_mask(feat, rule['conditions'])
        for t, pct, r, held in simulate(df, mask, rule['side'], int(rule.get('hold_hours', 24)),
                                        float(rule.get('stop_atr', 1.0)), feat['atr_d'].to_numpy(float)):
            rows.append((pair, t, pct, r, held))
    trades = pd.DataFrame(rows, columns=['pair', 't', 'pct', 'r', 'held'])
    out = {}
    for name in splits:
        a, b = (pd.Timestamp(x, tz='UTC') for x in SPLITS[name])
        k = trades[(trades['t'] >= a) & (trades['t'] < b)]
        weeks = (b - a).days / 7
        n = len(k)
        mean_r = k['r'].mean() if n else np.nan
        sd = k['r'].std(ddof=1) if n > 1 else np.nan
        out[name] = {'n': n, 'per_week': n / weeks, 'pct': k['pct'].mean() if n else np.nan,
                     'r': mean_r, 't': mean_r / sd * np.sqrt(n) if n > 1 and sd > 0 else np.nan,
                     'win': (k['r'] > 0).mean() if n else np.nan}
    return out, trades


def accepted(summary):
    tr, va = summary['train'], summary['valid']
    return (tr['n'] > 0 and tr['r'] > 0 and (tr['t'] or 0) >= 2.0 and tr['per_week'] >= 1.0
            and va['n'] > 0 and va['r'] > 0)


def line(name, summary, splits=('train', 'valid')):
    parts = []
    for s in splits:
        x = summary[s]
        parts.append(f"{s} {x['n']:4d} сд {x['per_week']:4.1f}/нед R {x['r']:+.3f} t {x['t']:+.1f} "
                     f"% {x['pct']:+.2f} плюс {x['win'] * 100 if x['n'] else 0:3.0f}%")
    return f'{name:44s} ' + ' | '.join(parts)
