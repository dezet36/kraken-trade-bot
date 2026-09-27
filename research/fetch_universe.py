"""
Широкий набор монет для замеров «между монетами»: дневные свечи и фандинг
всех бессрочных контрактов Bybit к USDT (linear), с 2021-06-01.

Зачем: импульс между монетами (research/ai_momentum_grid.py) найден на 20
парах кэшей замеров — тех же, на которых смотрелось всё остальное. Широкий
набор с отбором по обороту НА КАЖДУЮ ДАТУ (research/ai_momentum_universe.py)
проверяет, свойство ли это рынка или этих 20 монет.

Оговорка: Bybit отдаёт только ныне торгуемые контракты — умершие монеты
(LUNA, FTT и т.п.) в набор не попадают. Для импульса это скорее занижает
результат: умершие монеты падали и были бы в продаже.

Кладётся в research/universe_cache/:
    candles/<PAIR>_1d.csv   timestamp, open, high, low, close, volume, turnover
    funding/<PAIR>.csv      timestamp, funding_rate
Уже скачанное не перекачивается (удалите файл, чтобы обновить).

Запуск (порядок важен — фандинг качается с даты листинга):
    python research/fetch_universe.py listing    # даты листинга (первая дневная свеча) — возраст и старт фандинга
    python research/fetch_universe.py            # свечи всех, фандинг — тем, кто бывал в верхних 80 по обороту
"""
import os
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'research', 'universe_cache')
START = int(pd.Timestamp('2021-06-01', tz='UTC').value // 10 ** 6)
DAY = 86_400_000
PAUSE = 0.1
FUNDING_TOP = 80


def candles(client, symbol):
    rows, cursor = {}, START
    now = int(time.time() * 1000)
    while cursor < now:
        try:
            got = client.fetch_ohlcv(symbol, '1d', since=cursor, limit=1000)
        except Exception as exc:                          # noqa: BLE001
            print(f'      свечи сорвались: {str(exc)[:80]}', flush=True)
            break
        if not got:
            break
        for r in got:
            rows[int(r[0])] = r[1:6]
        newest = got[-1][0]
        if newest <= cursor:
            break
        cursor = newest + DAY
        time.sleep(PAUSE)
    return rows


def funding(client, symbol, start):
    """
    Окно до листинга Bybit отдаёт ПУСТЫМ (проверено 27.09.2026: SUI с
    2021-06-01 — 0 строк, с 2023-05-03 — 200), и первая версия на этом
    молча останавливалась: из 474 контрактов фандинг получили 17 старых.
    Поэтому старт — с даты листинга, а пустое окно перешагивается.
    """
    seen, cursor = {}, start
    now = int(time.time() * 1000)
    window = 200 * 8 * 3_600_000
    while cursor < now:
        try:
            rows = client.fetch_funding_rate_history(symbol, since=cursor, limit=200)
        except Exception as exc:                          # noqa: BLE001
            print(f'      фандинг сорвался: {str(exc)[:80]}', flush=True)
            break
        if not rows:
            cursor += window
            time.sleep(PAUSE)
            continue
        for r in rows:
            if r.get('timestamp') is not None and r.get('fundingRate') is not None:
                seen[int(r['timestamp'])] = float(r['fundingRate'])
        newest = max(r['timestamp'] for r in rows if r.get('timestamp'))
        if newest <= cursor:
            break
        cursor = newest + 1
        time.sleep(PAUSE)
    return seen


def main():
    import ccxt
    client = ccxt.bybit({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    markets = client.load_markets()
    symbols = sorted(s for s, m in markets.items()
                     if m.get('swap') and m.get('linear') and m.get('quote') == 'USDT'
                     and m.get('settle') == 'USDT' and m.get('active', True) and not m.get('expiry'))
    os.makedirs(os.path.join(OUT, 'candles'), exist_ok=True)
    os.makedirs(os.path.join(OUT, 'funding'), exist_ok=True)
    print(f'контрактов {len(symbols)}', flush=True)
    for n, symbol in enumerate(symbols):
        pair = markets[symbol]['id']
        path = os.path.join(OUT, 'candles', f'{pair}_1d.csv')
        if os.path.exists(path):
            continue
        rows = candles(client, symbol)
        if rows:
            frame = pd.DataFrame([[t, *v] for t, v in sorted(rows.items())],
                                 columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            frame['turnover'] = frame['volume'] * frame['close']
            frame['timestamp'] = pd.to_datetime(frame['timestamp'], unit='ms', utc=True)
            tmp = path + '.tmp'
            frame.to_csv(tmp, index=False)
            os.replace(tmp, path)
        if n % 50 == 0:
            print(f'  свечи: {n}/{len(symbols)}', flush=True)
    # Кто бывал в верхних FUNDING_TOP по обороту за 30 дней хоть в одну дату.
    turnover = {}
    for name in os.listdir(os.path.join(OUT, 'candles')):
        frame = pd.read_csv(os.path.join(OUT, 'candles', name))
        turnover[name[:-7]] = pd.Series(frame['turnover'].to_numpy(),
                                        index=pd.to_datetime(frame['timestamp'])).rolling(30).mean()
    wide = pd.DataFrame(turnover)
    ranks = wide.rank(axis=1, ascending=False)
    ever = sorted(c for c in wide.columns if (ranks[c] <= FUNDING_TOP).any())
    print(f'фандинг: {len(ever)} контрактов бывали в верхних {FUNDING_TOP}', flush=True)
    by_id = {m['id']: s for s, m in markets.items() if s in symbols}
    listed = {}
    listing_path = os.path.join(OUT, 'listing.csv')
    if os.path.exists(listing_path):
        frame = pd.read_csv(listing_path)
        listed = {p: int(pd.Timestamp(d, tz='UTC').value // 10 ** 6) for p, d in zip(frame['pair'], frame['first_day'])}
    for n, pair in enumerate(ever):
        path = os.path.join(OUT, 'funding', f'{pair}.csv')
        if os.path.exists(path) or pair not in by_id:
            continue
        seen = funding(client, by_id[pair], max(START, listed.get(pair, START)))
        if seen:
            frame = pd.DataFrame({'timestamp': list(seen), 'funding_rate': list(seen.values())}).sort_values('timestamp')
            frame['timestamp'] = pd.to_datetime(frame['timestamp'], unit='ms', utc=True)
            tmp = path + '.tmp'
            frame.to_csv(tmp, index=False)
            os.replace(tmp, path)
        if n % 20 == 0:
            print(f'  фандинг: {n}/{len(ever)}', flush=True)
    print('готово', flush=True)


def listing():
    """
    Дата первой дневной свечи каждого контракта — возраст на любую дату.
    Свечи качаются с 2021-06-01, и всё, что старше, выглядело бы листингом
    этого дня. Пишет universe_cache/listing.csv (pair, first_day).
    """
    import ccxt
    client = ccxt.bybit({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    markets = client.load_markets()
    since = int(pd.Timestamp('2018-01-01', tz='UTC').value // 10 ** 6)
    rows = []
    for symbol, m in sorted(markets.items()):
        if not (m.get('swap') and m.get('linear') and m.get('quote') == 'USDT' and m.get('settle') == 'USDT'
                and m.get('active', True) and not m.get('expiry')):
            continue
        try:
            got = client.fetch_ohlcv(symbol, '1d', since=since, limit=1)
        except Exception as exc:                          # noqa: BLE001
            print(f'  {m["id"]}: {str(exc)[:60]}', flush=True)
            continue
        if got:
            rows.append((m['id'], pd.Timestamp(got[0][0], unit='ms', tz='UTC').date().isoformat()))
        time.sleep(PAUSE)
    path = os.path.join(OUT, 'listing.csv')
    tmp = path + '.tmp'
    pd.DataFrame(rows, columns=['pair', 'first_day']).to_csv(tmp, index=False)
    os.replace(tmp, path)
    print(f'листинги: {len(rows)} контрактов → {path}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) > 1 and sys.argv[1] == 'listing':
        listing()
    else:
        main()
