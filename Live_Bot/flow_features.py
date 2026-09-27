"""
Признаки потока и позиционирования по часу — общий слой (27.09.2026).

Описывают рынок, а не решают, что с ним делать (CLAUDE.md, «Изоляция
стратегий»): ход цены, доля покупок по рынку (дельта агрессора, Binance),
доля счетов в лонге (Bybit), ОИ, фандинг, BTC. Считаются на ЗАКРЫТИИ часа
только по данным до него.

Формулы — один в один research/ai_pattern_lab.features: по ним на истории
искались и проверялись закономерности ИИ (docs/ИИ_замечания_на_проверку.md,
п. 68). Совпадение держит tests/test_flow_features.py. Менять расчёт — правка
общего слоя: с проверкой каждого, кто его читает.

Вход — часовая таблица пары (flow_data.frame): o, h, l, c, v, qv, trades, tb,
buy_ratio, oi, funding; индекс — открытие часа, UTC.
"""
import pandas as pd


def features(df, btc=None):
    c = df['c']
    out = pd.DataFrame(index=df.index)
    for h, name in ((1, 'ret_1h'), (4, 'ret_4h'), (24, 'ret_24h'), (72, 'ret_72h'), (168, 'ret_7d')):
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
        for h, name in ((4, 'btc_ret_4h'), (24, 'btc_ret_24h'), (168, 'btc_ret_7d')):
            out[name] = (bc / bc.shift(h) - 1) * 100
        out['rel_24h'] = out['ret_24h'] - out['btc_ret_24h']
    close_time = df.index + pd.Timedelta(hours=1)
    out['hour_utc'] = close_time.hour
    out['weekday'] = close_time.weekday
    return out


def cascade_count(masks, window=3):
    """
    Сколько пар за последние window часов были в состоянии mask (по каждому часу).

    masks — {пара: pd.Series[bool] по часам}. Нужна широте разгрузки: одиночный
    обвал монеты и разгрузка всего рынка — разные события (п. 68).
    """
    frame = pd.DataFrame(masks).fillna(False).astype(bool)
    return frame.rolling(window, min_periods=1).max().sum(axis=1)
