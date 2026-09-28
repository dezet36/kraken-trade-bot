"""
Сколько R стоит задержка входа ИИ-тетради (28.09.2026, docs п. 73).

Стенд входит на открытии часа после сигнала, вживую вход — через 5–15 минут:
данные закрытого часа, ответ модели (~1.5 мин), следующий цикл бота (до 5 мин).
Первая живая сделка (LINK, XLM 28.09) вошла через 7.3 мин. Если в первые минуты
после каскада цена уже отскакивает, задержка съедает часть преимущества.

Для каждого сигнала P1/P2/B3 на train и valid (начало серии, как в стенде): цена
на открытии часа и через 5, 10, 15 минут (открытие 5-минутной свечи Binance);
потеря в R = знак × (цена позже − цена на открытии) / цена на открытии / стоп.
Плюс — потеря, минус — выигрыш. 5-минутные свечи — в research/flow_cache_delay.

    python research/ai_entry_delay.py
"""
import json
import os
import sys
import time
import urllib.request

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_model_trader_bt as B                        # noqa: E402

CACHE = os.path.join(HERE, 'flow_cache_delay', 'opens_5m.json')
RESULT = os.path.join(HERE, 'results', 'ai_entry_delay.txt')
BINANCE = {'SHIB1000USDT': '1000SHIBUSDT'}
DELAYS = (5, 10, 15)


def opens_after(pair, start_ms, cache):
    """Открытия 5-минутных свечей от start_ms: [0, 5, 10, 15] минут."""
    key = f'{pair}:{start_ms}'
    if key not in cache:
        sym = BINANCE.get(pair, pair)
        url = (f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=5m'
               f'&startTime={start_ms}&limit=4')
        for attempt in range(3):
            try:
                rows = json.loads(urllib.request.urlopen(url, timeout=30).read())
                break
            except Exception:                         # noqa: BLE001
                time.sleep(2 + attempt * 3)
        else:
            rows = []
        cache[key] = [float(r[1]) for r in rows if int(r[0]) >= start_ms][:4]
        time.sleep(0.05)
    return cache[key]


def main():
    lines = []

    def say(text=''):
        print(text, flush=True)
        lines.append(text)

    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    cache = json.load(open(CACHE, encoding='utf-8')) if os.path.exists(CACHE) else {}
    data = B.prepare()
    rows = []
    for split in ('train', 'valid'):
        for t, items in B.alerts(data, split):
            start = int((t + pd.Timedelta(hours=1)).timestamp() * 1000)
            for p, key in items:
                pat = B.PATTERNS[key]
                atr = float(data[p][1].at[t, 'atr_d'])
                if not np.isfinite(atr) or atr <= 0:
                    continue
                o = opens_after(p, start, cache)
                if len(o) < 4:
                    continue
                sgn = 1.0 if pat['side'] == 'long' else -1.0
                dist = pat['stop'] * atr / 100.0
                row = {'split': split, 'key': key}
                for i, d in enumerate(DELAYS, start=1):
                    row[d] = sgn * (o[i] / o[0] - 1) / dist
                rows.append(row)
        json.dump(cache, open(CACHE, 'w', encoding='utf-8'))
    d = pd.DataFrame(rows)
    say('Цена задержки входа, R (плюс — потеря против входа на открытии часа)')
    for split in ('train', 'valid'):
        say(f'\n{split}:')
        for key in ('P1', 'P2', 'B3'):
            g = d[(d.split == split) & (d.key == key)]
            if not len(g):
                continue
            parts = ' | '.join(f'{m} мин {g[m].mean():+.3f} (t {g[m].mean() / g[m].std(ddof=1) * np.sqrt(len(g)):+.1f})'
                               for m in DELAYS)
            say(f'  {key}: {len(g):4d} сигналов — {parts}')
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
