"""
Настроение рынка — общий слой (30.09.2026): страх по опционам, спрос США, доля спота.

Три числа о рынке в целом, раз в закрытый час:
  dvol, dvol_chg_24h, dvol_pct_30d — ожидаемая волатильность BTC из опционов
      Deribit (DVOL): уровень, изменение за сутки, ранг за 30 дней (1 — самый
      большой страх месяца);
  btc_cb_prem_bp — премия Coinbase (USD) к споту Binance у BTC, б.п., среднее
      за 4 часа: плюс — покупатели США доплачивают;
  btc_spot_share_24h — доля спота в обороте BTC за сутки: спот / (спот +
      фьючерс Binance).
Формулы — как в research/ai_new_sources.py (там они проверялись как признаки
закономерностей, docs/ИИ_замечания_на_проверку.md, п. 82: торговых правил не
дали, но как фон анализа годятся); совпадение держит
tests/test_market_mood.py.

Описывает рынок, а не решает: пишет строку в DATA_DIR/market_mood.jsonl
(новый источник сначала логируется — CLAUDE.md, «Данные») и отдаёт последнюю
через facts(). Строка старше двух часов — прочерк, а не последнее известное.
Отказ источника — пустое поле и строка в журнал; торговля от этого не зависит.
"""
import json
import os
import time
import urllib.request

import pandas as pd

import config
from logger import log

H = 3_600_000
MAX_AGE_H = 2
_state = {'hour': None}


def path():
    return os.path.join(config.DATA_DIR, 'market_mood.jsonl')


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'kraken-bot'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _last_closed_hour(now_ms):
    return int(now_ms) // H * H - H


def compute(t, dvol=None, cb_close=None, spot_close=None, spot_qv=None, perp_qv=None):
    """
    Числа на час t (открытие закрытого часа) из часовых рядов, индекс — открытие часа (UTC).
    Та же арифметика, что research/ai_new_sources.enrich у BTC.
    """
    out = {}
    if dvol is not None and t in dvol.index:
        d = dvol[dvol.index <= t]
        out['dvol'] = float(d.iloc[-1])
        prev = t - pd.Timedelta(hours=24)
        out['dvol_chg_24h'] = (out['dvol'] / float(d.loc[prev]) - 1) * 100 if prev in d.index else None
        window = d.iloc[-720:]
        out['dvol_pct_30d'] = float(window.rank(pct=True).iloc[-1]) if len(window) >= 480 else None
    if cb_close is not None and spot_close is not None:
        prem = ((cb_close / spot_close) - 1) * 1e4
        last4 = prem[(prem.index <= t) & (prem.index > t - pd.Timedelta(hours=4))]
        out['btc_cb_prem_bp'] = float(last4.mean()) if last4.notna().sum() >= 3 else None
    if spot_qv is not None and perp_qv is not None:
        lo = t - pd.Timedelta(hours=24)
        s = spot_qv[(spot_qv.index > lo) & (spot_qv.index <= t)]
        p = perp_qv[(perp_qv.index > lo) & (perp_qv.index <= t)]
        if s.notna().sum() >= 20 and p.notna().sum() >= 20:
            out['btc_spot_share_24h'] = float(s.sum() / (s.sum() + p.sum()))
    return {k: (None if v is None or pd.isna(v) else round(v, 4)) for k, v in out.items()}


def _series(rows, ts_col, val_col, scale=1):
    s = pd.Series({int(r[ts_col]) * scale: float(r[val_col]) for r in rows}, dtype=float).sort_index()
    s.index = pd.to_datetime(s.index, unit='ms', utc=True)
    return s


def _fetch(t_ms):
    """Сырые ряды к часу t_ms; отказ источника — None по нему."""
    t = pd.Timestamp(t_ms, unit='ms', tz='UTC')
    got = {}
    try:
        start = t_ms - 31 * 24 * H
        r = _get(f'https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC'
                 f'&start_timestamp={start}&end_timestamp={t_ms + H - 1}&resolution=3600')['result']['data']
        got['dvol'] = _series(r, 0, 4)
    except Exception as exc:                          # noqa: BLE001
        log(f'   настроение рынка: DVOL не прочитан ({exc})')
    try:
        a = (t - pd.Timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')
        b = (t + pd.Timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        r = _get(f'https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600&start={a}&end={b}')
        got['cb_close'] = _series(r, 0, 4, scale=1000)
    except Exception as exc:                          # noqa: BLE001
        log(f'   настроение рынка: Coinbase не прочитан ({exc})')
    try:
        r = _get(f'https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&endTime={t_ms + H - 1}&limit=30')
        got['spot_close'] = _series(r, 0, 4)
        got['spot_qv'] = _series(r, 0, 7)
    except Exception as exc:                          # noqa: BLE001
        log(f'   настроение рынка: спот Binance не прочитан ({exc})')
    try:
        r = _get(f'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&endTime={t_ms + H - 1}&limit=30')
        got['perp_qv'] = _series(r, 0, 7)
    except Exception as exc:                          # noqa: BLE001
        log(f'   настроение рынка: фьючерс Binance не прочитан ({exc})')
    # Идущий час не в счёт: только закрытые (метка открытия ≤ t).
    return {k: v[v.index <= t] for k, v in got.items()}


def collect_if_due(now_ms=None, fetch=None):
    """Раз в закрытый час — строка в market_mood.jsonl. -> записанная строка или None."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    t_ms = _last_closed_hour(now_ms)
    if _state['hour'] is None:
        last = facts(now_ms=now_ms, max_age_h=10 ** 6)
        _state['hour'] = last.get('ts')
    if _state['hour'] == t_ms:
        return None
    _state['hour'] = t_ms
    try:
        row = {'ts': t_ms, 'at': pd.Timestamp(t_ms + H, unit='ms', tz='UTC').isoformat(),
               **compute(pd.Timestamp(t_ms, unit='ms', tz='UTC'), **(fetch or _fetch)(t_ms))}
        with open(path(), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(row) + '\n')
        return row
    except Exception as exc:                          # noqa: BLE001
        log(f'   настроение рынка: строка не записана ({exc})')
        return None


def facts(now_ms=None, max_age_h=MAX_AGE_H):
    """Последняя строка, если ей не больше max_age_h часов после закрытия часа; иначе {}."""
    try:
        with open(path(), encoding='utf-8') as fh:
            lines = fh.readlines()[-3:]
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        if now_ms - (int(row['ts']) + H) <= max_age_h * H:
            return row
        return {}
    return {}
