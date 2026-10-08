"""
Старая ФИБО вживую: быстрый или медленный откат к уровню входа — проверка
вперёд находки «медленный откат» (docs/ФИБО_разбор_сделок_2026-10-08.md,
разделы 5–6; решение владельца 08.10 — не менять, следить по ~100 новым
сделкам).

Время отката = от конца импульса (geometry.leg.to — закрытие часа экстремума
ноги, как видит сканер) до налива (open_time). На истории 2022–25 медленные
(≥ 3.5…8 ч) были лучше быстрых, в 2025-05…2026-07 — наоборот.

    scp -P 53547 root@195.133.35.5:/opt/kraken/bot_data/paper_trades.jsonl <папка>
    python research/fibo_retrace_live_check.py <папка>/paper_trades.jsonl [с какой даты, по умолчанию 2026-10-08]
"""
import json
import sys

import numpy as np
import pandas as pd

THRESHOLDS = (2.0, 3.5, 6.0, 8.0)


def main(path, since='2026-10-08'):
    t = pd.DataFrame([json.loads(l) for l in open(path, encoding='utf-8')])
    f = t[t.strategy == 'FIBO'].copy()
    f['r'] = pd.to_numeric(f.pnl_r, errors='coerce')
    f['open'] = pd.to_datetime(f.open_time, utc=True)
    geo = f.geometry.apply(lambda g: json.loads(g) if isinstance(g, str) and g.startswith('{') else {})
    leg_to = pd.to_datetime(geo.apply(lambda g: (g.get('leg') or {}).get('to')), utc=True)
    # leg.to — начало часа экстремума; закрытие часа — на час позже
    f['retrace_h'] = (f.open - (leg_to + pd.Timedelta(hours=1))).dt.total_seconds() / 3600
    for label, sub in (('все живые сделки', f), (f'с {since} (проверка вперёд)', f[f.open >= pd.Timestamp(since, tz='UTC')])):
        sub = sub.dropna(subset=['retrace_h', 'r'])
        print(f'\n{label}: {len(sub)} сделок, R на сделку {sub.r.mean():+.3f}' if len(sub) else f'\n{label}: сделок нет')
        if len(sub) < 2:
            continue
        for th in THRESHOLDS:
            fast, slow = sub[sub.retrace_h < th], sub[sub.retrace_h >= th]
            print(f'  порог {th:>3g} ч: быстрый откат {len(fast):3d} сд {fast.r.mean():+.3f} | '
                  f'медленный {len(slow):3d} сд {slow.r.mean():+.3f}')
        print('  шорты отдельно (как торгует бот с 27.09):')
        s = sub[sub.direction == 'SHORT']
        for th in (3.5, 6.0):
            fast, slow = s[s.retrace_h < th], s[s.retrace_h >= th]
            print(f'    порог {th:g} ч: быстрый {len(fast)} сд {fast.r.mean():+.3f} | медленный {len(slow)} сд {slow.r.mean():+.3f}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1], *(sys.argv[2:3] or []))
