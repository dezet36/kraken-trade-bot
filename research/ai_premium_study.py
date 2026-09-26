"""
Премия перпетуала к индексу — «толпа» в реальном времени — на пяти периодах.

ОТКУДА. Поток за неделю вживую (лента, ликвидации, ОИ, базис; 3547 пар-часов):
единственный признак с одним знаком в обеих половинах — премия: высокая
премия (толпа в лонгах доплачивает) → хуже следующие 4 часа (IC −0.17 и
−0.09). Фандинг считается из премии, и фандинг против толпы — самый
устойчивый признак отбора сетапов у всех каркасов (ai_filter_study). У
премии есть история (Bybit, часовые свечи индекса премии,
research/fetch_positioning.py) — здесь она проверяется как:

  1. самостоятельный сигнал: премия в крайних 5% своего распределения за
     30 дней → позиция против толпы на 4 и 24 ч, вход по следующей
     5-минутке, издержки тейкера и проскальзывание;
  2. признак отбора сетапов SMC, Фибо и доктрины ИИ: верхняя пятая часть
     против нижней по периодам (премия — в сторону сделки: плюс значит,
     что толпа стоит туда же, куда сделка).

Запуск:
    python research/ai_premium_study.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import ai_doctrine as D                               # noqa: E402
from common import ci, diff_ci                        # noqa: E402

H = 3_600_000
FEE_T, SLIP = 0.00055, 0.0005
FOLD = {'bear': 'bear', 'mid1': 'mid1', 'mid2': 'mid2', '12m': 'recent', 'fresh': 'recent'}


def load_premium(cache, pair):
    path = os.path.join(ROOT, 'research', cache, 'premium', f'{pair}_1h.csv')
    if not os.path.exists(path):
        return None
    frame = pd.read_csv(path)
    stamps = pd.to_datetime(frame['timestamp'], utc=True)
    ts = ((stamps - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy(dtype='int64')
    order = np.argsort(ts)
    return ts[order], frame['premium'].to_numpy(dtype=float)[order]


def standalone(q=0.05, holds=(4, 24)):
    rows = []
    for period, cache in D.PERIODS.items():
        for pair in D.PAIRS:
            pr = load_premium(cache, pair)
            a5 = D.load(cache, pair, '5m')
            if pr is None or a5 is None:
                continue
            ts, val = pr
            s = pd.Series(val)
            lo = s.rolling(720, min_periods=500).quantile(q).shift(1).to_numpy()
            hi = s.rolling(720, min_periods=500).quantile(1 - q).shift(1).to_numpy()
            ts5 = a5[:, 0]
            busy = -1
            for k in range(len(ts)):
                if not np.isfinite(lo[k]) or ts[k] < busy:
                    continue
                side = -1 if val[k] > hi[k] else (1 if val[k] < lo[k] else 0)
                if side == 0:
                    continue
                t_in = ts[k] + H                          # свеча премии закрылась
                j = int(np.searchsorted(ts5, t_in))
                if j >= len(a5):
                    continue
                entry = a5[j, 1] * (1 + SLIP * side)
                row = {'period': period, 'pair': pair, 't': ts[k], 'side': side}
                for h in holds:
                    e = int(np.searchsorted(ts5, t_in + h * H)) - 1
                    if e <= j or e >= len(a5):
                        row[f'h{h}'] = np.nan
                        continue
                    out = a5[e, 4] * (1 - SLIP * side)
                    row[f'h{h}'] = ((out / entry - 1) * side - 2 * FEE_T) * 100
                rows.append(row)
                busy = t_in + max(holds) * H
    f = pd.DataFrame(rows)
    f['fold'] = f.period.map(FOLD)
    f = f.drop_duplicates(subset=['pair', 't'])
    print(f'1. ПРЕМИЯ В КРАЙНИХ {q:.0%} ЗА 30 ДНЕЙ → ПРОТИВ ТОЛПЫ; чистыми, % цены; событий {len(f)}')
    for h in holds:
        col = f'h{h}'
        cells = [f'{fold} {f[f.fold == fold][col].mean():+.3f} ({(f.fold == fold).sum()})'
                 for fold in ('bear', 'mid1', 'mid2', 'recent')]
        v = f[col].dropna().to_numpy()
        lo_, hi_ = ci(v)
        print(f'   {h:>2d} ч: ' + ', '.join(cells) + f' | все {v.mean():+.3f}% [{lo_:+.3f}; {hi_:+.3f}]')
        for side, name in ((1, 'лонг (толпа в шортах)'), (-1, 'шорт (толпа в лонгах)')):
            g = f[f.side == side][col].dropna()
            print(f'        {name:24s} {g.mean():+.3f}% ({len(g)})')


def as_filter():
    import glob
    frames = [pd.read_pickle(p) for p in sorted(glob.glob(os.path.join(HERE, 'results', 'ai_setups_*.pkl')))]
    data = pd.concat(frames, ignore_index=True)
    data['fold'] = data['period'].map(FOLD)
    data = data[data.filled & data.r.notna()].drop_duplicates(subset=['skeleton', 'pair', 't', 'side'])
    cache_of = {p: c for p, c in D.PERIODS.items()}
    prem = {}
    vals = []
    for period, pair, t, side in zip(data.period, data.pair, data.t, data.side):
        key = (period, pair)
        if key not in prem:
            prem[key] = load_premium(cache_of[period], pair)
        pr = prem[key]
        if pr is None:
            vals.append((np.nan, np.nan))
            continue
        ts, val = pr
        k = int(np.searchsorted(ts, t - H, side='right')) - 1   # закрытая часовая свеча премии
        sgn = 1 if side == 'LONG' else -1
        if k < 24:
            vals.append((np.nan, np.nan))
            continue
        vals.append((val[k] * 1e4 * sgn, val[k - 23:k + 1].mean() * 1e4 * sgn))
    data['prem_1h'] = [v[0] for v in vals]
    data['prem_24h'] = [v[1] for v in vals]
    print('\n2. ПРЕМИЯ КАК ПРИЗНАК ОТБОРА (в сторону сделки): верхняя пятая часть минус нижняя, R на сделку')
    for skeleton in ('smc', 'fibo', 'doctrine'):
        g = data[data.skeleton == skeleton]
        for col in ('prem_1h', 'prem_24h'):
            cells, tops, bots = [], [], []
            for fold in ('bear', 'mid1', 'mid2', 'recent'):
                part = g[(g.fold == fold) & g[col].notna()]
                if len(part) < 60:
                    cells.append('   —   ')
                    continue
                lo, hi = part[col].quantile([0.2, 0.8])
                top, bot = part[part[col] >= hi].r.to_numpy(), part[part[col] <= lo].r.to_numpy()
                cells.append(f'{top.mean() - bot.mean():+.3f}')
                tops.append(top)
                bots.append(bot)
            if tops:
                lo_, hi_ = diff_ci(np.concatenate(tops), np.concatenate(bots))
                d = np.concatenate(tops).mean() - np.concatenate(bots).mean()
                print(f'   {skeleton:9s} {col:9s} ' + ' '.join(f'{c:>8s}' for c in cells)
                      + f'  | все {d:+.3f} [{lo_:+.3f}; {hi_:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    standalone()
    as_filter()
