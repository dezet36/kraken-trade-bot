"""
Свечи свежего периода для всех 20 пар списка бота (research/ai_doctrine.PAIRS).

ЗАЧЕМ (30.09.2026). В backtest_cache_fresh лежат только 10 пар пула SMC, а
владелец просит статистику SMC на всех парах. Докачиваем остальные в ОТДЕЛЬНУЮ
папку backtest_cache_fresh20: общий кэш не трогаем — research/rsibb_live_sim
перебирает файлы backtest_cache_fresh, и лишние пары молча поменяли бы его
замер. Пары, которые уже есть, копируются как есть; новые качаются с Bybit за
тот же отрезок (1h и 5m — формат backtest_smc.load_cached; 4h и 1d
backtest_smc.load_pair собирает из часовых).

Запуск:
    python research/fetch_fresh20.py
"""
import os
import pickle
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SRC = os.path.join(HERE, 'backtest_cache_fresh')
DST = os.path.join(HERE, 'backtest_cache_fresh20')
FRAMES = {'1h': 3_600_000, '5m': 300_000}          # 4h и 1d загрузчик собирает из 1h


def bounds(tf):
    """Первая и последняя метка этого ТФ в исходном кэше (по BTC)."""
    with open(os.path.join(SRC, f'BTCUSDT_{tf}.pkl'), 'rb') as fh:
        raw = pickle.load(fh)
    stamps = sorted(int(r[0]) for r in raw)
    return stamps[0], stamps[-1]


def download(client, pair, tf, since, until):
    step = FRAMES[tf]
    out, cursor = {}, since
    while cursor <= until:
        for attempt in range(5):
            try:
                rows = client.fetch_ohlcv(pair, tf, since=cursor, limit=1000)
                break
            except Exception as exc:                          # noqa: BLE001
                print(f'    {pair} {tf}: {exc} — повтор', flush=True)
                time.sleep(2 + attempt * 3)
        else:
            raise RuntimeError(f'{pair} {tf}: не скачалось')
        if not rows:
            cursor += 1000 * step
            continue
        for r in rows:
            if since <= r[0] <= until:
                out[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:6]]
        nxt = int(rows[-1][0]) + step
        if nxt <= cursor:
            break
        cursor = nxt
    return [out[k] for k in sorted(out)]


def main():
    import ai_doctrine as D
    import ccxt
    os.makedirs(DST, exist_ok=True)
    client = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
    have = {f.rsplit('_', 1)[0] for f in os.listdir(SRC) if f.endswith('_5m.pkl')}
    for pair in D.PAIRS:
        for tf in FRAMES:
            target = os.path.join(DST, f'{pair}_{tf}.pkl')
            if os.path.exists(target):
                continue
            if pair in have:
                shutil.copyfile(os.path.join(SRC, f'{pair}_{tf}.pkl'), target)
                print(f'  {pair} {tf}: скопирован', flush=True)
                continue
            since, until = bounds(tf)
            rows = download(client, pair.replace('USDT', '/USDT:USDT'), tf, since, until)
            with open(target + '.tmp', 'wb') as fh:
                pickle.dump(rows, fh)
            os.replace(target + '.tmp', target)
            print(f'  {pair} {tf}: скачано {len(rows)} свечей', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
