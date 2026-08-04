"""
Загрузка данных медвежьего рынка и боковика для проверки смены режима.

Зачем: весь бэктест проекта сделан на периоде 2025-05 .. 2026-07, который
был преимущественно бычьим. Обе стратегии почти всё время держали лонги.
Это означает, что о поведении в падающем и в боковом рынке мы не знаем
НИЧЕГО — а деплой на реальные деньги переживёт смену режима обязательно.

Период 2022-01 .. 2023-06 закрывает сразу три режима:
    2022 H1  — обвал (LUNA в мае, серия ликвидаций);
    2022 H2  — падение и крах FTX в ноябре;
    2023 H1  — восстановление и длинный боковик.

Пары берём только те, что существовали в 2022 году: HYPE, WIF, 1000PEPE,
TAO, PUMPFUN, ASTER тогда ещё не торговались.

Данные кладутся в отдельный кэш, формат совпадает с backtest_cache_12m,
поэтому все существующие скрипты работают с ним без изменений.

Запуск:
    python research/fetch_bear_data.py
"""

import os
import pickle
import sys
import time
from datetime import datetime, timezone

import ccxt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, 'research', 'backtest_cache_bear')

# Пары, торговавшиеся на Bybit linear в начале 2022 года
BEAR_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT',
    'LINKUSDT', 'BNBUSDT', 'AVAXUSDT', 'LTCUSDT', 'DOTUSDT', 'BCHUSDT',
    'UNIUSDT', 'XLMUSDT',
]

TIMEFRAMES = ['1h', '4h', '5m']
SINCE = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
UNTIL = int(datetime(2023, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)

_TF_MS = {'5m': 5 * 60_000, '15m': 15 * 60_000, '1h': 3_600_000, '4h': 4 * 3_600_000}


def fetch_range(exchange, symbol, timeframe, since_ms, until_ms, label=''):
    """Качает диапазон свечей с продолжением после сбоев."""
    cache_file = os.path.join(CACHE_DIR, f'{symbol}_{timeframe}.pkl')
    if os.path.exists(cache_file):
        with open(cache_file, 'rb') as fh:
            data = pickle.load(fh)
        print(f'  [кэш] {symbol} {timeframe}: {len(data)} свечей', flush=True)
        return data

    tf_ms = _TF_MS[timeframe]
    seen, out = set(), []
    cursor, fails = since_ms, 0

    print(f'  [качаю] {label} {symbol} {timeframe}...', end='', flush=True)
    while cursor < until_ms:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            fails = 0
        except ccxt.BadSymbol:
            print(' СИМВОЛА НЕТ', flush=True)
            return None
        except Exception as exc:
            fails += 1
            if fails > 8:
                print(f' СДАЮСЬ ({exc})', flush=True)
                return None
            time.sleep(3)
            continue

        if not batch:
            break
        added = 0
        for candle in batch:
            if candle[0] not in seen and candle[0] < until_ms:
                seen.add(candle[0])
                out.append(candle)
                added += 1
        if added == 0:
            break
        cursor = batch[-1][0] + tf_ms
        time.sleep(0.25)

    out.sort(key=lambda c: c[0])
    if not out:
        print(' ПУСТО', flush=True)
        return None

    first = datetime.fromtimestamp(out[0][0] / 1000, timezone.utc).date()
    last = datetime.fromtimestamp(out[-1][0] / 1000, timezone.utc).date()
    print(f' {len(out)} свечей ({first} .. {last})', flush=True)

    with open(cache_file, 'wb') as fh:
        pickle.dump(out, fh)
    return out


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    exchange = ccxt.bybit({'options': {'defaultType': 'linear'}, 'enableRateLimit': True})

    print(f'Период: 2022-01-01 .. 2023-07-01 (обвал, крах FTX, боковик)')
    print(f'Пары: {len(BEAR_PAIRS)}, кэш: {CACHE_DIR}\n')

    ok, missing = [], []
    for i, pair in enumerate(BEAR_PAIRS, 1):
        label = f'{i}/{len(BEAR_PAIRS)}'
        got_all = True
        for timeframe in TIMEFRAMES:
            data = fetch_range(exchange, pair, timeframe, SINCE, UNTIL, label)
            if data is None or len(data) < 100:
                got_all = False
                break
        (ok if got_all else missing).append(pair)

    print(f'\nГотово: {len(ok)} пар с полным набором данных')
    if missing:
        print(f'Без данных за период: {", ".join(missing)}')
    print('\nДальше: прогнать обе стратегии на этом кэше и сравнить с бычьим периодом.')


if __name__ == '__main__':
    main()
