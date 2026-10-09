"""
Интервал выплат фандинга у монет ИИ-тетради — сейчас и в периодах проверки (30.09.2026, п. 81).

У части монет Bybit платит фандинг раз в 4 ч (иногда раз в 1–2 ч), и ставка за
выплату у них меньше; правило P4 сравнивает ставку за выплату с −2 bp
(сосед live-bot-4b, research/smc_explore3._interval_scale). Здесь — какой
интервал у 42 монет тетради сейчас (instruments-info, fundingInterval) и какая
доля выплат шла с интервалом ≤ 4 ч в 2021, train, valid и test (funding/history).

    python research/ai_funding_intervals.py
"""
import json
import os
import sys
import time
import urllib.request

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'Live_Bot'))
PERIODS = {'2021': ('2021-01-01', '2022-01-01'), 'train': ('2022-01-01', '2024-07-01'),
           'valid': ('2024-07-01', '2025-05-01'), 'test': ('2025-05-01', '2026-10-01')}


def get(url):
    for n in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except Exception:                              # noqa: BLE001
            time.sleep(1 + n)
    raise RuntimeError(url)


def payments(pair, since='2021-01-01'):
    stop, out, end = pd.Timestamp(since, tz='UTC').timestamp() * 1000, set(), int(time.time() * 1000)
    while True:
        batch = get(f'https://api.bybit.com/v5/market/funding/history?category=linear&symbol={pair}'
                    f'&limit=200&endTime={end}')['result']['list']
        if not batch:
            break
        out |= {int(x['fundingRateTimestamp']) for x in batch}
        oldest = min(int(x['fundingRateTimestamp']) for x in batch)
        if oldest >= end or oldest < stop:
            break
        end = oldest - 1
        time.sleep(0.05)
    return pd.to_datetime(sorted(out), unit='ms', utc=True)


def main():
    os.environ.setdefault('BOT_DATA_DIR', os.path.join(HERE, 'results'))
    from strategies.llm import llm_notebook as N
    inst = {x['symbol']: x for x in
            get('https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000')['result']['list']}
    lines = ['Интервал выплат фандинга у 42 монет тетради: сейчас (ч) и доля выплат с интервалом ≤ 4 ч по периодам',
             f"{'пара':14s} {'группа':6s} {'сейчас':>6s} " + ' '.join(f'{k:>8s}' for k in PERIODS)]
    for p in N.UNIVERSE:
        now_h = int(inst.get(p, {}).get('fundingInterval') or 0) / 60
        t = payments(p)
        gaps = pd.Series((t[1:] - t[:-1]).total_seconds() / 3600, index=t[1:])
        cells = []
        for a, b in PERIODS.values():
            g = gaps[(gaps.index >= a) & (gaps.index < b)]
            cells.append('—' if not len(g) else ('8ч' if not (g <= 4.01).any() else f'{(g <= 4.01).mean():.0%}'))
        group = 'core' if p in N.POOL else ('extra' if p in N.EXTRA else 'new')
        lines.append(f'{p:14s} {group:6s} {now_h:6.0f} ' + ' '.join(f'{c:>8s}' for c in cells))
    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', 'ai_funding_intervals.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
