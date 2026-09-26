"""
История позиционирования под кэши замеров: открытый интерес (1h), фандинг и премия перпетуала к индексу (1h).

Кладётся рядом со свечами кэша:
    <кэш>/open_interest/<PAIR>_1h.csv   timestamp, open_interest
    <кэш>/funding/<PAIR>.csv            timestamp, funding_rate

Открытый интерес Bybit отдаёт только шагом КОНЦОМ окна (`until`): `since`
молча игнорируется и возвращает последние 200 записей — так дважды делался
ложный вывод «истории нет» (память проекта, exchange-history-depth). Фандинг
отдаётся обычным `since`. Лонг/шорт (стена 8.3 суток) и дельта агрессора
(истории нет вовсе) здесь не качаются — их можно только копить живьём.

Уже скачанное не перекачивается. Прежний загрузчик (fetch_open_interest.py)
удалён вместе с замерами 20.09.2026; формат файлов тот же.

Запуск:
    python research/fetch_positioning.py              # все кэши
    python research/fetch_positioning.py mid1 mid2    # выбранные
"""
import os
import pickle
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOUR = 3_600_000
PAUSE = 0.12

CACHES = {
    'bear': 'backtest_cache_bear',
    'mid1': 'backtest_cache_mid1',
    'mid2': 'backtest_cache_mid2',
    '12m': 'backtest_cache_12m',
    'fresh': 'backtest_cache_fresh',
}
PAIRS = ['XRPUSDT', 'ARBUSDT', 'SUIUSDT', 'ADAUSDT', 'UNIUSDT', 'DOGEUSDT', 'ETHUSDT',
         'BTCUSDT', 'SOLUSDT', 'ZECUSDT', 'AAVEUSDT', 'DOTUSDT', 'BNBUSDT', 'XLMUSDT',
         'LTCUSDT', 'LINKUSDT', 'AVAXUSDT', 'SHIB1000USDT', 'NEARUSDT', 'COTIUSDT']


def span(cache, pair):
    path = os.path.join(ROOT, 'research', cache, f'{pair}_1h.pkl')
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        rows = pickle.load(fh)
    return (int(rows[0][0]), int(rows[-1][0])) if rows else None


def open_interest(client, symbol, start, end):
    seen, edge = {}, end
    while edge > start:
        try:
            rows = client.fetch_open_interest_history(symbol, '1h', limit=200, params={'until': edge})
        except Exception as exc:                          # noqa: BLE001
            print(f'      ОИ сорвалось: {str(exc)[:80]}')
            break
        stamps = [r['timestamp'] for r in rows or [] if r.get('timestamp')]
        if not stamps:
            break
        for r in rows:
            value = r.get('openInterestAmount')
            if value is None:
                value = r.get('openInterestValue')
            if r.get('timestamp') is not None and value is not None:
                seen[int(r['timestamp'])] = float(value)
        oldest = min(stamps)
        if oldest >= edge - HOUR:                       # биржа упёрлась в предел хранения
            break
        edge = oldest - 1
        time.sleep(PAUSE)
    return {t: v for t, v in seen.items() if start <= t <= end}


def funding(client, symbol, start, end):
    seen, cursor = {}, start
    while cursor < end:
        try:
            rows = client.fetch_funding_rate_history(symbol, since=cursor, limit=200)
        except Exception as exc:                          # noqa: BLE001
            print(f'      фандинг сорвался: {str(exc)[:80]}')
            break
        if not rows:
            break
        for r in rows:
            if r.get('timestamp') is not None and r.get('fundingRate') is not None:
                seen[int(r['timestamp'])] = float(r['fundingRate'])
        newest = max(r['timestamp'] for r in rows if r.get('timestamp'))
        if newest <= cursor:
            break
        cursor = newest + 1
        time.sleep(PAUSE)
    return {t: v for t, v in seen.items() if start - 8 * HOUR <= t <= end}


def premium(client, symbol, start, end):
    """Премия перпетуала к индексу (часовые свечи индекса премии), закрытие часа."""
    seen, cursor = {}, start
    while cursor < end:
        try:
            rows = client.fetch_premium_index_ohlcv(symbol, '1h', since=cursor, limit=1000)
        except Exception as exc:                          # noqa: BLE001
            print(f'      премия сорвалась: {str(exc)[:80]}')
            break
        if not rows:
            break
        for r in rows:
            if r[0] is not None and r[4] is not None:
                seen[int(r[0])] = float(r[4])
        newest = rows[-1][0]
        if newest <= cursor:
            break
        cursor = newest + HOUR
        time.sleep(PAUSE)
    return {t: v for t, v in seen.items() if start <= t <= end}


def save(path, column, found):
    frame = pd.DataFrame({'timestamp': list(found), column: list(found.values())}).sort_values('timestamp')
    frame['timestamp'] = pd.to_datetime(frame['timestamp'], unit='ms', utc=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frame.to_csv(path, index=False)
    return len(frame)


def main(names):
    import ccxt
    client = ccxt.bybit({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    client.load_markets()
    for name in names:
        cache = CACHES[name]
        print(f'=== {name} ({cache})', flush=True)
        for pair in PAIRS:
            got = span(cache, pair)
            if got is None:
                continue
            start, end = got
            symbol = pair[:-4] + '/USDT:USDT'
            if symbol not in client.markets:
                print(f'  {pair:<13} нет рынка')
                continue
            oi_path = os.path.join(ROOT, 'research', cache, 'open_interest', f'{pair}_1h.csv')
            fr_path = os.path.join(ROOT, 'research', cache, 'funding', f'{pair}.csv')
            line = f'  {pair:<13}'
            if not os.path.exists(oi_path):
                found = open_interest(client, symbol, start, end)
                n = save(oi_path, 'open_interest', found) if found else 0
                line += f' ОИ {n:>5} ({n / max(1, (end - start) / HOUR) * 100:5.1f}%)'
            else:
                line += ' ОИ есть'
            if not os.path.exists(fr_path):
                found = funding(client, symbol, start, end)
                n = save(fr_path, 'funding_rate', found) if found else 0
                line += f'   фандинг {n:>5}'
            else:
                line += '   фандинг есть'
            pr_path = os.path.join(ROOT, 'research', cache, 'premium', f'{pair}_1h.csv')
            if not os.path.exists(pr_path):
                found = premium(client, symbol, start, end)
                n = save(pr_path, 'premium', found) if found else 0
                line += f'   премия {n:>5} ({n / max(1, (end - start) / HOUR) * 100:5.1f}%)'
            else:
                line += '   премия есть'
            print(line, flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1:] or list(CACHES))
