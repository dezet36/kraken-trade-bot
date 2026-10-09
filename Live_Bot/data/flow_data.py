"""
Часовые данные потока и позиционирования по парам — общий слой (27.09.2026).

Сырые данные с бирж, как есть (CLAUDE.md, «Изоляция стратегий», п. 1):
    Binance, часовые свечи фьючерса: o, h, l, c, v, qv, trades, tb (объём
        покупок по рынку — дельта агрессора; у Bybit её истории нет);
    Bybit: доля счетов в лонге (account-ratio, 1 ч), ОИ (1 ч), фандинг.
Источники те же, что у research/ai_flow_fetch.py — на них искались и
проверялись закономерности ИИ (п. 68); формат строки тот же.

Как работает. Таблица пары — последние HISTORY_H ЗАКРЫТЫХ часов (идущий час
отбрасывается: признак не должен видеть свечу, которой ещё нет). Раз в новый
час докачиваются последние часы и дописываются; закрытые строки пишутся в
DATA_DIR/flow/<пара>.jsonl — новый источник сначала логируется (CLAUDE.md,
«Данные»). Отказ источника — столбец остаётся пустым (признаки — NaN, сигнала
нет) и строка в журнал; торговля от него не зависит.
"""
import json
import os
import time

import pandas as pd

from infra import config
from data import sources
from infra.logger import log

H = 3_600_000
HISTORY_H = 1000          # окнам признаков нужно до 744 ч (30 дней + сутки)
BINANCE = {'SHIB1000USDT': '1000SHIBUSDT'}
COLUMNS = ['o', 'h', 'l', 'c', 'v', 'qv', 'trades', 'tb', 'tbq', 'buy_ratio', 'oi', 'funding']

_raw = {}                 # пара -> DataFrame, индекс — мс открытия часа
_frames = {}              # пара -> та же таблица с индексом-временем (UTC), для признаков
_last_hour = {}           # пара -> метка последнего закрытого часа в таблице (мс)


def last_closed_hour(now_ms=None):
    """Метка открытия последнего ЗАКРЫТОГО часа (мс)."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return now_ms // H * H - H


def _binance(pair, start_ms, end_ms):
    sym = BINANCE.get(pair, pair)
    rows, start = [], start_ms
    while start <= end_ms:
        # Адрес — из реестра источников (data/sources.py), путь — здесь.
        batch = sources.get('binance_futures', f'/fapi/v1/klines?symbol={sym}&interval=1h'
                            f'&startTime={start}&endTime={end_ms + H - 1}&limit=1500')
        if not batch:
            break
        rows += batch
        start = batch[-1][0] + H
    df = pd.DataFrame(rows, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'close_ts', 'qv', 'trades', 'tb', 'tbq', 'x'])
    df = df[df['ts'] <= end_ms].drop(columns=['close_ts', 'x']).drop_duplicates('ts')
    for col in ('o', 'h', 'l', 'c', 'v', 'qv', 'tb', 'tbq'):
        df[col] = df[col].astype(float)
    df['trades'] = df['trades'].astype(int)
    return df.set_index('ts')


def _bybit_ratio(pair, start_ms, end_ms):
    out, start = {}, start_ms
    while start <= end_ms:
        stop = min(start + 499 * H, end_ms)
        r = sources.get('bybit', f'/v5/market/account-ratio?category=linear&symbol={pair}&period=1h'
                        f'&limit=500&startTime={start}&endTime={stop}')
        for x in (r.get('result') or {}).get('list') or []:
            out[int(x['timestamp'])] = float(x['buyRatio'])
        start = stop + H
    return pd.Series(out, dtype=float).sort_index()


def _bybit_back(path_of, key_ts, key_val, start_ms, end_ms):
    out, end = {}, end_ms
    while end >= start_ms:
        batch = (sources.get('bybit', path_of(end)).get('result') or {}).get('list') or []
        if not batch:
            break
        for x in batch:
            out[int(x[key_ts])] = float(x[key_val])
        oldest = min(int(x[key_ts]) for x in batch)
        if oldest >= end:
            break
        end = oldest - 1
    return pd.Series(out, dtype=float).sort_index()


def _fetch(pair, start_ms, end_ms):
    """Сырые часы пары [start, end] (метки открытия). Отказ источника — пустой столбец."""
    df = _binance(pair, start_ms, end_ms)
    if df.empty:
        return df
    try:
        df['buy_ratio'] = _bybit_ratio(pair, start_ms, end_ms).reindex(df.index)
    except Exception as exc:                          # noqa: BLE001
        log(f'   flow {pair}: доля счетов в лонге не прочитана ({exc})')
        df['buy_ratio'] = float('nan')
    try:
        oi = _bybit_back(lambda e: f'/v5/market/open-interest?category=linear&symbol={pair}'
                                   f'&intervalTime=1h&limit=200&endTime={e}',
                         'timestamp', 'openInterest', start_ms, end_ms)
        df['oi'] = oi.reindex(df.index)
    except Exception as exc:                          # noqa: BLE001
        log(f'   flow {pair}: ОИ не прочитан ({exc})')
        df['oi'] = float('nan')
    try:
        # Фандинг выплачивается раз в 8 ч: берём с запасом назад и протягиваем
        # вперёд с момента выплаты — будущего в строке нет.
        fr = _bybit_back(lambda e: f'/v5/market/funding/history?category=linear'
                                   f'&symbol={pair}&limit=200&endTime={e}',
                         'fundingRateTimestamp', 'fundingRate', start_ms - 9 * H, end_ms)
        df['funding'] = fr.reindex(df.index, method='ffill') if len(fr) else float('nan')
    except Exception as exc:                          # noqa: BLE001
        log(f'   flow {pair}: фандинг не прочитан ({exc})')
        df['funding'] = float('nan')
    return df[COLUMNS]


def _log_rows(pair, df):
    path = os.path.join(config.DATA_DIR, 'flow', f'{pair}.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as fh:
        for ts, row in df.iterrows():
            rec = {'ts': int(ts)}
            rec.update({k: (None if pd.isna(v) else (int(v) if k == 'trades' else float(v))) for k, v in row.items()})
            fh.write(json.dumps(rec) + '\n')


def frame(pair, now_ms=None):
    """
    Таблица пары: последние HISTORY_H закрытых часов, индекс — открытие часа (UTC).
    None — если Binance не ответил и истории нет.
    """
    end = last_closed_hour(now_ms)
    have = _raw.get(pair)
    last = _last_hour.get(pair)
    if have is not None and last == end:
        return _frames[pair]
    try:
        if have is None or last is None or end - last > 48 * H:
            fresh = _fetch(pair, end - (HISTORY_H - 1) * H, end)
            new = fresh
        else:
            # Докачка с перекрытием в 3 часа: ОИ и доля счетов приходят с опозданием.
            start = last - 3 * H
            fresh = _fetch(pair, start, end)
            new = pd.concat([have[have.index < start], fresh])
        if new.empty:
            return _frames.get(pair)
        new = new[~new.index.duplicated(keep='last')].sort_index().iloc[-HISTORY_H:]
        # В журнал — только новые закрытые часы; при первом чтении — последний.
        _log_rows(pair, fresh[fresh.index > last] if last is not None else fresh.iloc[-1:])
        _raw[pair] = new
        view = new.copy()
        view.index = pd.to_datetime(view.index, unit='ms', utc=True)
        _frames[pair] = view
        _last_hour[pair] = end
        return view
    except Exception as exc:                          # noqa: BLE001
        log(f'   flow {pair}: данные не получены ({exc})')
        return _frames.get(pair)
