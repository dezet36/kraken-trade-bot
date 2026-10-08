"""
Доминация USDT по дням, 2020-09 … сегодня — для проверки «вход по направлению
доминации» на истории (docs/ФИБО_старая_идеи_2026-10-08.md).

Бот считает USDT.D сам (Live_Bot/market_cap.py, топ-125 монет), но хранит
только 30 дней. Для истории:
  капа USDT — DefiLlama (stablecoins.llama.fi/stablecoin/1, обращающееся
              предложение по дням; цена USDT ≈ $1);
  TOTAL     — CoinMarketCap (публичный data-api global-metrics, суточные).
USDT.D = капа USDT ÷ TOTAL. Метка дня — его начало (00:00 UTC); читать значение
дня D можно только с D+1 00:00 (оба источника публикуют итог дня после него).

    python research/usdt_dominance_fetch.py  → research/market_dominance/usdt_d_daily.csv
"""
import os
import time

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'market_dominance', 'usdt_d_daily.csv')
UA = {'User-Agent': 'Mozilla/5.0'}


def usdt_supply():
    j = requests.get('https://stablecoins.llama.fi/stablecoin/1', timeout=60, headers=UA).json()
    rows = [(int(t['date']), float((t.get('circulating') or {}).get('peggedUSD') or 0)) for t in j['tokens']]
    s = pd.Series({pd.Timestamp(d, unit='s', tz='UTC').normalize(): v for d, v in rows})
    return s[s > 0].sort_index()


def total_mcap(start='2020-08-01'):
    out = {}
    t0 = int(pd.Timestamp(start, tz='UTC').timestamp())
    now = int(time.time())
    while t0 < now:
        t1 = min(t0 + 300 * 86400, now)
        url = ('https://api.coinmarketcap.com/data-api/v3/global-metrics/quotes/historical'
               f'?format=chart&interval=1d&timeStart={t0}&timeEnd={t1}')
        for attempt in range(5):
            try:
                q = requests.get(url, timeout=60, headers=UA).json()['data']['quotes']
                break
            except Exception:                              # noqa: BLE001
                time.sleep(3 + 3 * attempt)
        else:
            raise RuntimeError(f'CMC не отдал {t0}…{t1}')
        for x in q:
            d = pd.Timestamp(x['timestamp']).tz_convert('UTC').normalize()
            out[d] = (float(x['quote'][0]['totalMarketCap']), float(x.get('btcDominance') or 0))
        t0 = t1
        time.sleep(1)
    df = pd.DataFrame.from_dict(out, orient='index', columns=['total_mcap', 'btc_d']).sort_index()
    return df


def main():
    sup = usdt_supply()
    tot = total_mcap()
    df = tot.join(sup.rename('usdt_mcap'), how='inner')
    df['usdt_d'] = df.usdt_mcap / df.total_mcap * 100
    df.index.name = 'day'
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    df.to_csv(OUT + '.tmp')
    os.replace(OUT + '.tmp', OUT)
    print(f'{OUT}: {len(df)} дней, {df.index[0].date()} … {df.index[-1].date()}')
    print(df.iloc[[0, len(df) // 2, -1]].round(3).to_string())


if __name__ == '__main__':
    main()
