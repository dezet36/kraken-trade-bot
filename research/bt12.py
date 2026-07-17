"""
bt12.py — слой 12-месячных данных для walk-forward и квалификации пар.

Отдельный кэш backtest_cache_12m/ (старый backtest_cache/ не трогаем —
воспроизводимость прежних 6-месячных результатов). Параметризация backtest.py —
monkeypatch модульных глобалей (bt.CACHE_DIR/MONTHS_BACK/BACKTEST_PAIRS читаются
в рантайме — проверено).

DAYS_HISTORY=400: окно оценки 360 дней + ~40 дней прогрева HTF (EMA200 на 4H =
220 свечей ≈ 36.7 дня) — иначе первый месяц торговался бы с HTF=NEUTRAL.

Запуск загрузки: python bt12.py   (resumable: готовые pkl = мгновенный кэш-хит)
"""
import json
import os
import pickle
from datetime import datetime, timezone, timedelta

import backtest as bt
import backtest_campaign as camp

CACHE_12M    = r'D:\Bot trade\research\backtest_cache_12m'
DAYS_HISTORY = 400
EVAL_DAYS    = 360

# ≥400 дней истории на Bybit linear (проверено load_markets 2026-07-04)
CORE_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT', 'ADAUSDT',
    'AVAXUSDT', 'DOTUSDT', 'LINKUSDT', 'LTCUSDT', 'BCHUSDT', 'TRXUSDT', 'XLMUSDT',
    'UNIUSDT', 'APTUSDT', 'SUIUSDT', 'ARBUSDT', 'OPUSDT', 'TIAUSDT', 'TAOUSDT',
    'HYPEUSDT', 'JUPUSDT', 'WIFUSDT', 'LDOUSDT', 'XMRUSDT', 'ZECUSDT',
]
YOUNG_PAIRS = ['ASTERUSDT', 'LITUSDT']                  # 9.5 / 6.1 мес — качаем сколько есть
# Кандидаты на ВКЛЮЧЕНИЕ (реальные контракты вместо мёртвых тикеров пула;
# микроценовая причина бана исчезает в 1000-номинале)
CANDIDATE_PAIRS = ['1000PEPEUSDT', 'SHIB1000USDT', '1000BONKUSDT', '1000FLOKIUSDT',
                   'PUMPFUNUSDT']
# Записи живого пула, которых НЕ существует на Bybit linear (критерий 0 квалификации)
DEAD_SYMBOLS = ['PEPEUSDT', 'SHIBUSDT', 'BONKUSDT', 'FLOKIUSDT', 'PUMPUSDT']
# Старые 10 пар — для walk-forward (сопоставимость с прежними результатами)
PAIRS10 = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT',
           'AVAXUSDT', 'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'LTCUSDT']

ALL_PAIRS = CORE_PAIRS + YOUNG_PAIRS + CANDIDATE_PAIRS


def apply(pairs):
    """Перенаправляет backtest.py на 12м-кэш и заданный список пар."""
    bt.CACHE_DIR = CACHE_12M
    bt.MONTHS_BACK = DAYS_HISTORY / 30
    bt.BACKTEST_PAIRS = list(pairs)
    os.makedirs(CACHE_12M, exist_ok=True)


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def _linear_market(ex, pair_id):
    """Возвращает описание active linear perpetual по биржевому id или None."""
    for m in ex.markets.values():
        if m.get('id') == pair_id and m.get('linear') and m.get('swap') and m.get('active'):
            return m
    return None


def ensure_cache(pairs=None):
    """Скачивает 1h/5m/4h для пар (кэш-хиты пропускаются). Пишет manifest.json."""
    pairs = list(pairs) if pairs else ALL_PAIRS
    apply(pairs)
    ex = bt.get_exchange()
    ex.load_markets()
    since_glob = int((datetime.now(timezone.utc) - timedelta(days=DAYS_HISTORY)).timestamp() * 1000)

    manifest, skipped = {}, []
    for i, pair in enumerate(pairs):
        m = _linear_market(ex, pair)
        if m is None:
            skipped.append(pair)
            print(f'[SKIP] {pair}: нет на Bybit linear')
            continue
        launch = int(m.get('info', {}).get('launchTime') or 0)
        since = max(since_glob, launch)
        try:
            for tf in ('1h', '5m', '4h'):
                raw = bt.fetch_ohlcv_full(ex, pair, tf, since,
                                          label=f'{i+1}/{len(pairs)}', max_retries=8)
                manifest.setdefault(pair, {})[tf] = (
                    {'n': len(raw), 'first': _iso(raw[0][0]), 'last': _iso(raw[-1][0])}
                    if raw else {'n': 0})
        except RuntimeError as e:
            skipped.append(pair)
            print(f'[SKIP] {pair}: {e}')
            continue

    with open(os.path.join(CACHE_12M, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump({'downloaded_at': datetime.now(timezone.utc).isoformat(),
                   'days': DAYS_HISTORY, 'pairs': manifest, 'skipped': skipped},
                  f, ensure_ascii=False, indent=2)
    print(f'\nМанифест: {len(manifest)} пар OK, skipped: {skipped or "нет"}')
    return manifest, skipped


def load(pairs):
    """Все пары разом (для walk-forward по 10 парам). После ensure_cache — кэш-хиты."""
    apply(pairs)
    return bt.load_all_data()


def load_pair(pair):
    """Одна пара (для квалификации — щадящий RAM-профиль). Только кэш-хиты."""
    apply([pair])
    d1 = bt.to_df(bt.fetch_ohlcv_full(None, pair, '1h', 0))
    d5 = bt.to_df(bt.fetch_ohlcv_full(None, pair, '5m', 0))
    d4 = bt.to_df(bt.fetch_ohlcv_full(None, pair, '4h', 0))
    return d1, d5, d4


def sim_trades(pair, df1, df5, df4h, cfg, config_key):
    """Кэш результатов симуляций: (пара, конфиг) считается один раз.
    ВАЖНО: config_key обязан кодировать ВСЕ setattr-ручки (ответственность вызывающего)."""
    tdir = os.path.join(CACHE_12M, 'trades')
    f = os.path.join(tdir, f'{config_key}__{pair}.pkl')
    if os.path.exists(f):
        with open(f, 'rb') as fh:
            return pickle.load(fh)
    tr = camp.simulate_pair_campaign(df1, df5, df4h, pair, cfg)
    os.makedirs(tdir, exist_ok=True)
    with open(f, 'wb') as fh:
        pickle.dump(tr, fh)
    return tr


if __name__ == '__main__':
    import time
    t0 = time.time()
    ensure_cache(ALL_PAIRS)
    print(f'Время: {(time.time()-t0)/60:.1f} мин')
