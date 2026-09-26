"""
Может ли фильтр (модель, формула или ИИ) отобрать лучшие сетапы по тем
данным, что видит ИИ? Замер вне выборки на четырёх независимых периодах.

ПОСТАНОВКА. Каждый сетап каркаса (research/ai_setups.py) — строка признаков
на момент постановки и исход в R. Фильтр учится на трёх периодах и решает на
четвёртом, которого не видел. Если отобранная им доля не лучше всех сетапов
того же периода — ни устойчиво, ни в сумме, — в этих признаках нет того, чем
фильтр мог бы отобрать лучшее, и языковая модель, читающая те же числа, этого
тоже не сможет.

Периоды-свёртки: bear, mid1, mid2 и recent (12m + fresh без повторов: fresh
почти целиком лежит внутри 12m, и считать их раздельно значило бы учить и
проверять на одних сделках).

Три замера:
  1. каждый признак отдельно: верхняя пятая часть против нижней, знак в
     каждом периоде и общий интервал;
  2. фильтр вне выборки: логистическая регрессия (исход > 0) и гребневая
     (R) с L2, обучение на трёх периодах, отбор верхней доли на четвёртом;
     сравнение с «брать всё» и со случайным отбором той же доли;
  3. события: сетапы в сутках вокруг решения ФРС против остальных.

Запуск:
    python research/ai_filter_study.py
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import ci, diff_ci                     # noqa: E402

RNG = np.random.default_rng(20260926)
FOLDS = ('bear', 'mid1', 'mid2', 'recent')

NUMERIC = ['ret_4h', 'ret_24h', 'ret_7d', 'ret_30d', 'pos30', 'atr_pct', 'atr_rank', 'adr_pct',
           'vol_ratio', 'dist_entry_pct', 'stop_pct', 'stop_adr', 'rr1', 'tp_adr', 'ema50d_gap',
           'er_30d', 'btc_ret_24h', 'btc_ret_7d', 'btc_er_30d', 'oi_chg_24h', 'oi_chg_4h',
           'oi_x_price', 'funding', 'funding_3', 'fomc_hours']

# Решения ФРС (день публикации, 14:00 по Нью-Йорку ≈ 18–19 UTC).
FOMC = ['2022-01-26', '2022-03-16', '2022-05-04', '2022-06-15', '2022-07-27', '2022-09-21',
        '2022-11-02', '2022-12-14', '2023-02-01', '2023-03-22', '2023-05-03', '2023-06-14',
        '2023-07-26', '2023-09-20', '2023-11-01', '2023-12-13', '2024-01-31', '2024-03-20',
        '2024-05-01', '2024-06-12', '2024-07-31', '2024-09-18', '2024-11-07', '2024-12-18',
        '2025-01-29', '2025-03-19', '2025-05-07', '2025-06-18', '2025-07-30', '2025-09-17',
        '2025-10-29', '2025-12-10', '2026-01-28', '2026-03-18', '2026-04-29', '2026-06-17',
        '2026-07-29', '2026-09-16']
FOMC_MS = np.array([int(pd.Timestamp(d + ' 18:30', tz='UTC').value // 10 ** 6) for d in FOMC])


def load():
    frames = [pd.read_pickle(p) for p in sorted(glob.glob(os.path.join(HERE, 'results', 'ai_setups_*.pkl')))]
    data = pd.concat(frames, ignore_index=True)
    data['fold'] = data['period'].replace({'12m': 'recent', 'fresh': 'recent'})
    data = data.drop_duplicates(subset=['skeleton', 'pair', 't', 'side'], keep='first')
    t = data['t'].to_numpy(dtype='int64')
    k = np.searchsorted(FOMC_MS, t)
    prev = np.abs(t - FOMC_MS[np.clip(k - 1, 0, len(FOMC_MS) - 1)])
    nxt = np.abs(FOMC_MS[np.clip(k, 0, len(FOMC_MS) - 1)] - t)
    data['fomc_hours'] = np.minimum(prev, nxt) / 3_600_000
    data['session'] = pd.cut(data['hour'], [-1, 7, 12, 20, 23], labels=['азия', 'лондон', 'нью-йорк', 'поздно'])
    return data


# ── 1. Признаки по одному ────────────────────────────────────────────────────
def single_features(trades, label):
    print(f'\n1. ПРИЗНАКИ ПО ОДНОМУ — {label}: верхняя пятая часть минус нижняя, R на сделку')
    print(f'   {"признак":15s} ' + ' '.join(f'{f:>8s}' for f in FOLDS) + '   все: разность [95%]      знак')
    found = []
    for name in NUMERIC:
        if name not in trades or trades[name].notna().sum() < 200:
            continue
        cells, tops, bottoms = [], [], []
        for fold in FOLDS:
            part = trades[(trades.fold == fold) & trades[name].notna()]
            if len(part) < 60:
                cells.append(np.nan)
                continue
            lo, hi = part[name].quantile([0.2, 0.8])
            top, bottom = part[part[name] >= hi].r.to_numpy(), part[part[name] <= lo].r.to_numpy()
            cells.append(top.mean() - bottom.mean())
            tops.append(top)
            bottoms.append(bottom)
        if not tops:
            continue
        lo_ci, hi_ci = diff_ci(np.concatenate(tops), np.concatenate(bottoms))
        diff = np.concatenate(tops).mean() - np.concatenate(bottoms).mean()
        signs = [np.sign(c) for c in cells if np.isfinite(c)]
        same = len(signs) >= 3 and (all(s > 0 for s in signs) or all(s < 0 for s in signs))
        mark = 'ДЕРЖИТСЯ' if same and (lo_ci > 0 or hi_ci < 0) else ('знак везде один' if same else '')
        print(f'   {name:15s} ' + ' '.join(f'{c:+8.3f}' if np.isfinite(c) else f'{"—":>8s}' for c in cells)
              + f'   {diff:+.3f} [{lo_ci:+.3f}; {hi_ci:+.3f}]  {mark}')
        if mark == 'ДЕРЖИТСЯ':
            found.append(name)
    return found


# ── 2. Фильтр вне выборки ────────────────────────────────────────────────────
def design(frame, columns, stats=None):
    x = frame[columns].astype(float).to_numpy()
    if stats is None:
        med = np.nanmedian(x, axis=0)
        med = np.where(np.isfinite(med), med, 0.0)
        filled = np.where(np.isfinite(x), x, med)
        # Хвосты признаков режутся по обучающим квантилям: иначе один выброс
        # (ОИ +300% за сутки у новой монеты) раздавит масштаб всех остальных.
        lo, hi = np.nanpercentile(filled, 1, axis=0), np.nanpercentile(filled, 99, axis=0)
        clipped = np.clip(filled, lo, hi)
        mu, sd = clipped.mean(axis=0), clipped.std(axis=0)
        sd = np.where(sd > 0, sd, 1.0)
        stats = (med, lo, hi, mu, sd)
    med, lo, hi, mu, sd = stats
    filled = np.where(np.isfinite(x), x, med)
    z = (np.clip(filled, lo, hi) - mu) / sd
    side = (frame['side'].to_numpy() == 'LONG').astype(float)[:, None]
    return np.hstack([np.ones((len(z), 1)), z, side]), stats


def fit_logistic(x, y, l2=5.0, steps=40):
    w = np.zeros(x.shape[1])
    penalty = np.full(x.shape[1], l2)
    penalty[0] = 0.0
    for _ in range(steps):
        p = 1 / (1 + np.exp(-(x @ w)))
        grad = x.T @ (p - y) + penalty * w
        hess = (x * (p * (1 - p))[:, None]).T @ x + np.diag(penalty + 1e-6)
        w -= np.linalg.solve(hess, grad)
    return w


def fit_ridge(x, y, l2=50.0):
    penalty = np.full(x.shape[1], l2)
    penalty[0] = 0.0
    return np.linalg.solve(x.T @ x + np.diag(penalty), x.T @ y)


def out_of_sample(trades, columns, label, share=0.5):
    print(f'\n2. ФИЛЬТР ВНЕ ВЫБОРКИ — {label}: учится на трёх периодах, отбирает {share:.0%} на четвёртом')
    print(f'   {"период":8s} {"сделок":>6s} {"все":>8s} {"логист.":>8s} {"гребн.":>8s} {"случайно (5–95%)":>20s}')
    picked_log, picked_ridge, everything = [], [], []
    for fold in FOLDS:
        train, test = trades[trades.fold != fold], trades[trades.fold == fold]
        if len(test) < 40 or len(train) < 200:
            continue
        xt, stats = design(train, columns)
        xs, _ = design(test, columns, stats)
        w_log = fit_logistic(xt, (train.r.to_numpy() > 0).astype(float))
        w_ridge = fit_ridge(xt, np.clip(train.r.to_numpy(), -2, 6))
        k = max(1, int(round(len(test) * share)))
        r = test.r.to_numpy()
        top_log = r[np.argsort(-(xs @ w_log))[:k]]
        top_ridge = r[np.argsort(-(xs @ w_ridge))[:k]]
        rand = np.array([RNG.choice(r, k, replace=False).mean() for _ in range(2000)])
        print(f'   {fold:8s} {len(test):6d} {r.mean():+8.3f} {top_log.mean():+8.3f} {top_ridge.mean():+8.3f} '
              f'   [{np.percentile(rand, 5):+.3f}; {np.percentile(rand, 95):+.3f}]')
        picked_log.append(top_log)
        picked_ridge.append(top_ridge)
        everything.append(r)
    if not everything:
        return
    allr = np.concatenate(everything)
    for name, picked in (('логистическая', picked_log), ('гребневая', picked_ridge)):
        p = np.concatenate(picked)
        lo, hi = ci(p)
        print(f'   {name:13s}: отобрано {len(p)}, {p.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}] против всех {allr.mean():+.3f}R')


# ── 3. События ───────────────────────────────────────────────────────────────
def events(trades, label):
    near = trades[trades.fomc_hours <= 24]
    far = trades[trades.fomc_hours > 24]
    lo, hi = diff_ci(near.r.to_numpy(), far.r.to_numpy())
    print(f'\n3. РЕШЕНИЕ ФРС ±24 ч — {label}: рядом {len(near)} сделок {near.r.mean():+.3f}R, '
          f'остальные {len(far)} {far.r.mean():+.3f}R, разность [{lo:+.3f}; {hi:+.3f}]')
    by = trades.groupby('session', observed=True).r.agg(['count', 'mean'])
    print('   по сессиям входа: ' + ', '.join(f'{s} {int(row["count"])} сд {row["mean"]:+.3f}R'
                                           for s, row in by.iterrows()))


def agreement(data):
    """
    Согласие каркасов: сетап одной стратегии, когда другая в ту же сторону по
    той же паре ставила заявку за последние сутки, — лучше ли он? Так мог бы
    работать «совет» из нескольких стратегий, которым руководит ИИ.
    """
    print('\n4. СОГЛАСИЕ КАРКАСОВ (та же пара и сторона, заявка другого каркаса за 24 ч до)')
    day = 24 * 3_600_000
    for own, other in (('smc', 'fibo'), ('fibo', 'smc'), ('smc', 'doctrine'), ('doctrine', 'smc')):
        mine = data[(data.skeleton == own) & data.filled & data.r.notna()]
        theirs = data[data.skeleton == other]
        if mine.empty or theirs.empty:
            continue
        index = {}
        for (pair, side), g in theirs.groupby(['pair', 'side']):
            index[(pair, side)] = np.sort(g.t.to_numpy())
        flags = []
        for pair, side, t in zip(mine.pair, mine.side, mine.t):
            ts = index.get((pair, side))
            if ts is None:
                flags.append(False)
                continue
            k = np.searchsorted(ts, t, side='right')
            flags.append(k > 0 and t - ts[k - 1] <= day)
        flags = np.array(flags)
        yes, no = mine.r.to_numpy()[flags], mine.r.to_numpy()[~flags]
        if len(yes) < 20:
            print(f'   {own} при заявке {other}: всего {len(yes)} сделок — мало')
            continue
        lo, hi = diff_ci(yes, no)
        cells = []
        for fold in FOLDS:
            m = (mine.fold == fold).to_numpy()
            if (m & flags).sum() >= 10:
                cells.append(f'{fold} {mine.r.to_numpy()[m & flags].mean() - mine.r.to_numpy()[m & ~flags].mean():+.2f}')
        print(f'   {own} при заявке {other}: {len(yes)} сд {yes.mean():+.3f}R, без неё {len(no)} сд {no.mean():+.3f}R, '
              f'разность [{lo:+.3f}; {hi:+.3f}]; по периодам: ' + ', '.join(cells))


def main():
    data = load()
    agreement(data)
    for skeleton, label in (('smc', 'SMC (каждая заявка отдельно)'), ('fibo', 'Фибо (живые заявки, каждая отдельно)'),
                            ('doctrine', 'доктрина ИИ без модели')):
        if not ((data.skeleton == skeleton) & data.filled).any():
            continue
        trades = data[(data.skeleton == skeleton) & data.filled & data.r.notna()].copy()
        print(f'\n══════ {label}: {len(trades)} сделок ══════')
        print('   по периодам: ' + ', '.join(f'{f} {int((trades.fold == f).sum())} сд '
                                            f'{trades[trades.fold == f].r.mean():+.3f}R' for f in FOLDS))
        found = single_features(trades, label)
        base = [c for c in NUMERIC if c in trades and trades[c].notna().mean() > 0.5]
        out_of_sample(trades, base, label + ', все признаки')
        if found:
            out_of_sample(trades, found, label + f', только устойчивые ({", ".join(found)})')
        out_of_sample(trades, base, label + ', все признаки', share=0.3)
        events(trades, label)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
