"""
Есть ли в данных, которые видит ИИ, НАПРАВЛЕНИЕ? Замер вне выборки.

ЗАЧЕМ. Модель на живой неделе угадывала направление на сутки в 41% разборов
(research/ai_live_plans.py, docs п. 58). Это может быть слабостью модели, а
может — свойством данных: если в них нет направления, его не извлечёт никто.
Здесь вместо модели — простейшая статистика на тех же признаках (ход цены за
4 ч…30 дней, место в 30-дневном диапазоне, волатильность, объём, наклон
дневной средней, BTC, открытый интерес, фандинг), обученная на трёх периодах
и проверенная на четвёртом.

Два вопроса:
  по времени     — угадывает ли прогноз знак хода пары за 4 ч / сутки / 3 суток;
  между парами   — обгоняют ли верхние три пары по прогнозу нижние три за
                   сутки (рынок в целом гасится). Издержки двух кругов:
                   тейкер 0.22% на пару сделок, мейкер 0.08% (research/
                   relative_strength.py до 20.09 считал так же).

Точки решения — 00:00, 08:00 и 16:00 UTC каждого дня (закрытые свечи).

Запуск:
    python research/ai_direction_study.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_doctrine as D                               # noqa: E402
import ai_setups as S                                 # noqa: E402
from ai_filter_study import design, fit_ridge         # noqa: E402
from common import ci                                 # noqa: E402

H = 3_600_000
FOLDS = ('bear', 'mid1', 'mid2', 'recent')
FEATS = ['ret_4h', 'ret_24h', 'ret_7d', 'ret_30d', 'pos30', 'atr_pct', 'atr_rank', 'adr_pct', 'vol_ratio',
         'ema50d_gap', 'er_30d', 'btc_ret_24h', 'btc_ret_7d', 'btc_er_30d', 'oi_chg_24h', 'oi_chg_4h',
         'oi_x_price', 'funding', 'funding_3']
HORIZONS = (4, 24, 72)
TAKER_PAIR, MAKER_PAIR = 0.22, 0.08                 # % на пару сделок лонг+шорт, круг туда-обратно


def build(period):
    cache = D.PERIODS[period]
    btc = D.load(cache, 'BTCUSDT', '1h')
    rows = []
    for pair in D.PAIRS:
        a1 = D.load(cache, pair, '1h')
        if a1 is None:
            continue
        oi = S.load_series(cache, 'open_interest', pair, 'open_interest')
        fr = S.load_series(cache, 'funding', pair, 'funding_rate')
        c = a1[:, 4]
        for i in range(24 * 32, len(a1) - 72):
            t_close = int(a1[i, 0]) + H
            if (t_close // H) % 8:
                continue
            close = c[i]
            row = S.features(a1, t_close, 'LONG', close, close * 0.98, close * 1.04, btc, oi, fr)
            if row is None:
                continue
            for h in HORIZONS:
                row[f'fwd_{h}h'] = (c[i + h] / close - 1) * 100
            row.update({'period': period, 'pair': pair, 't': t_close})
            rows.append(row)
    return pd.DataFrame(rows)


def load():
    frames = []
    for period in D.PERIODS:
        path = os.path.join(HERE, 'results', f'ai_direction_{period}.pkl')
        if not os.path.exists(path):
            build(period).to_pickle(path)
        frames.append(pd.read_pickle(path))
    data = pd.concat(frames, ignore_index=True)
    data['fold'] = data['period'].replace({'12m': 'recent', 'fresh': 'recent'})
    data = data.drop_duplicates(subset=['pair', 't'], keep='first')
    data['side'] = 'LONG'
    return data


def spearman(a, b):
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])


def time_series(data, cols, h):
    target = f'fwd_{h}h'
    print(f'\n  по времени, ход за {h} ч:')
    print(f'   {"период":8s} {"точек":>6s} {"IC прогноза":>11s} {"угадан знак":>11s} {"ход по прогнозу, %":>18s} '
          f'{"моментум 24ч IC":>15s}')
    signed_all = []
    for fold in FOLDS:
        train, test = data[data.fold != fold], data[data.fold == fold]
        xt, stats = design(train, cols)
        xs, _ = design(test, cols, stats)
        # Цель обрезана по квантилям обучения: один обвал на 40% иначе
        # задаёт весь наклон гребневой регрессии.
        lo, hi = train[target].quantile([0.01, 0.99])
        w = fit_ridge(xt, train[target].clip(lo, hi).to_numpy(), l2=200.0)
        pred = xs @ w
        y = test[target].to_numpy()
        signed = np.sign(pred) * y
        signed_all.append(signed)
        print(f'   {fold:8s} {len(test):6d} {spearman(pred, y):+11.3f} {np.mean(np.sign(pred) == np.sign(y)) * 100:10.1f}% '
              f'{signed.mean():+18.3f} {spearman(test.ret_24h.to_numpy(), y):+15.3f}')
    s = np.concatenate(signed_all)
    lo, hi = ci(s)
    print(f'   все: ход в сторону прогноза {s.mean():+.3f}% [{lo:+.3f}; {hi:+.3f}] при круге тейкером 0.11%')


def cross_section(data, cols, h=24, k=3):
    target = f'fwd_{h}h'
    print(f'\n  между парами, сутки: верхние {k} минус нижние {k} по прогнозу (рынок гасится)')
    print(f'   {"период":8s} {"дней":>5s} {"прогноз, %":>11s} {"моментум 24ч":>13s} {"случайно (5–95%)":>20s}')
    spreads_all = []
    for fold in FOLDS:
        train, test = data[data.fold != fold], data[data.fold == fold]
        xt, stats = design(train, cols)
        lo, hi = train[target].quantile([0.01, 0.99])
        w = fit_ridge(xt, train[target].clip(lo, hi).to_numpy(), l2=200.0)
        test = test.copy()
        test['pred'] = design(test, cols, stats)[0] @ w
        spreads, mom, rand = [], [], []
        # Сутки не перекрываются: решение только в 00:00 UTC.
        for t, day in test[(test.t // H) % 24 == 0].groupby('t'):
            if len(day) < 2 * k + 2:
                continue
            day = day.sort_values('pred')
            spreads.append(day[target].iloc[-k:].mean() - day[target].iloc[:k].mean())
            m = day.sort_values('ret_24h')
            mom.append(m[target].iloc[-k:].mean() - m[target].iloc[:k].mean())
            shuffled = day[target].sample(frac=1.0, random_state=int(t % 2 ** 31)).to_numpy()
            rand.append(shuffled[-k:].mean() - shuffled[:k].mean())
        spreads, rand = np.array(spreads), np.array(rand)
        boot = [RNG.choice(rand, len(rand)).mean() for _ in range(1000)]
        spreads_all.append(spreads)
        print(f'   {fold:8s} {len(spreads):5d} {spreads.mean():+11.3f} {np.mean(mom):+13.3f} '
              f'   [{np.percentile(boot, 5):+.3f}; {np.percentile(boot, 95):+.3f}]')
    s = np.concatenate(spreads_all)
    lo, hi = ci(s)
    print(f'   все: {s.mean():+.3f}% в сутки [{lo:+.3f}; {hi:+.3f}]; круг лонг+шорт тейкером {TAKER_PAIR}%, '
          f'мейкером {MAKER_PAIR}%')


RNG = np.random.default_rng(20260926)


def main():
    data = load()
    print(f'точек решения: {len(data)}; по периодам: ' +
          ', '.join(f'{f} {int((data.fold == f).sum())}' for f in FOLDS))
    cols = [c for c in FEATS if c in data and data[c].notna().mean() > 0.5]
    print('признаки:', ', '.join(cols))
    for h in HORIZONS:
        time_series(data, cols, h)
    cross_section(data, cols)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
