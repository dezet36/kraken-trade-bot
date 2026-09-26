"""
Может ли МОДЕЛЬ по данным биржи понять, куда пойдёт рынок? Самая сильная
честная проверка этой идеи на истории.

ЗАЧЕМ. Замысел владельца: модель читает всё, что бот берёт с биржи, понимает
направление и торгует по нему. Языковая модель не учится на исходах, поэтому
проверяется не она, а верхняя граница: обученная нелинейная модель
(гистограммный градиентный бустинг) на ВСЕХ признаках, у которых есть история, —
ход цены на семи горизонтах, волатильность, место в диапазонах, средние,
объём, открытый интерес, фандинг, премия перпетуала, BTC, ширина рынка,
ранги монет между собой. Если и она вне выборки не находит направления,
которое перекрывает издержки, его нет в этих данных, и языковая модель его
тоже не найдёт. Если находит — это и есть то, что модель должна «понимать».

Проверка строго вперёд по времени: учится на прошлых периодах, торгует
следующий (bear+mid1 → mid2; bear+mid1+mid2 → recent), и отдельно —
«оставить один период» для устойчивости. Точки решения — каждые 4 часа.
Торговля: позиция на горизонт по знаку прогноза только в сильнейших 20%
прогнозов (порог — по обучению), издержки круга тейкером 0.11% +
проскальзывание 0.1%.

Данных стакана и ленты в истории нет — их проверка только по накопленным
живым записям бота.

Запуск:
    python research/ai_direction_ml.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import ai_doctrine as D                               # noqa: E402
from ai_premium_study import load_premium            # noqa: E402
from common import ci                                 # noqa: E402

H = 3_600_000
STEP_H = 4
HORIZONS = (4, 24)
COST_PCT = 0.21
FOLD = {'bear': 'bear', 'mid1': 'mid1', 'mid2': 'mid2', '12m': 'recent', 'fresh': 'recent'}


def load_series(cache, sub, pair, column):
    name = f'{pair}_1h.csv' if sub in ('open_interest', 'premium') else f'{pair}.csv'
    path = os.path.join(ROOT, 'research', cache, sub, name)
    if not os.path.exists(path):
        return None
    frame = pd.read_csv(path)
    stamps = pd.to_datetime(frame['timestamp'], utc=True)
    ts = ((stamps - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).astype('int64')
    return pd.Series(frame[column].to_numpy(dtype=float), index=ts.to_numpy()).sort_index()


def pair_frame(cache, pair):
    a1 = D.load(cache, pair, '1h')
    if a1 is None:
        return None
    f = pd.DataFrame(a1[:, :6], columns=['ts', 'o', 'h', 'l', 'c', 'v'])
    f['ts'] = f['ts'].astype('int64')
    f['t'] = f['ts'] + H                           # момент решения — закрытие часа
    c = f['c']
    for h in (1, 4, 12, 24, 72, 168, 720):
        f[f'ret_{h}'] = (c / c.shift(h) - 1) * 100
    tr = np.maximum(f['h'] - f['l'], np.maximum((f['h'] - c.shift()).abs(), (f['l'] - c.shift()).abs()))
    f['atr'] = tr.rolling(14).mean() / c * 100
    f['atr_rank'] = f['atr'].rolling(720, min_periods=300).rank(pct=True)
    f['vol24'] = np.log(c).diff().rolling(24).std() * 100
    for h in (24, 168, 720):
        hi, lo = f['h'].rolling(h).max(), f['l'].rolling(h).min()
        f[f'pos_{h}'] = (c - lo) / (hi - lo)
    for n in (20, 50, 200):
        f[f'ema{n}'] = (c / c.ewm(span=n, adjust=False).mean() - 1) * 100
    daily = c.ewm(span=50 * 24, adjust=False).mean()
    f['ema50d'] = (c / daily - 1) * 100
    path = c.diff().abs().rolling(720).sum()
    f['er30'] = (c - c.shift(720)).abs() / path
    f['vol_ratio'] = f['v'].rolling(24).sum() / (f['v'].rolling(168).sum() / 7)
    f['vol_z1'] = (f['v'] - f['v'].rolling(168).mean()) / f['v'].rolling(168).std()
    rng = (f['h'] - f['l']).replace(0, np.nan)
    f['wick_up'] = (f['h'] - np.maximum(f['o'], c)) / rng
    f['wick_dn'] = (np.minimum(f['o'], c) - f['l']) / rng
    f['hour'] = (f['t'] // H) % 24
    f['weekday'] = ((f['t'] // (24 * H)) + 3) % 7
    # Позиционирование: только значения, уже известные к моменту решения.
    oi = load_series(cache, 'open_interest', pair, 'open_interest')
    if oi is not None:
        s = oi.reindex(f['ts'].to_numpy(), method='ffill')    # ОИ на открытии часа решения
        s.index = f.index
        for h in (4, 24, 168):
            f[f'oi_{h}'] = (s / s.shift(h) - 1) * 100
        f['oi_z'] = (s - s.rolling(720, min_periods=200).mean()) / s.rolling(720, min_periods=200).std()
        f['oi_x_price'] = f['oi_24'] * np.sign(f['ret_24'])
    fr = load_series(cache, 'funding', pair, 'funding_rate')
    if fr is not None:
        s = fr.reindex(f['t'].to_numpy(), method='ffill')     # выплаченный к моменту решения
        s.index = f.index
        f['fund'] = s * 1e4
        f['fund_z'] = (s - s.rolling(720, min_periods=200).mean()) / s.rolling(720, min_periods=200).std()
    pr = load_premium(cache, pair)
    if pr is not None:
        ser = pd.Series(pr[1], index=pr[0]).sort_index()
        s = ser.reindex(f['ts'].to_numpy(), method='ffill')    # премия закрытого часа
        s.index = f.index
        f['prem'] = s * 1e4
        f['prem24'] = f['prem'].rolling(24).mean()
        f['prem_z'] = (f['prem'] - f['prem'].rolling(720, min_periods=200).mean()) / f['prem'].rolling(720, min_periods=200).std()
    for h in HORIZONS:
        f[f'fwd_{h}'] = (c.shift(-h) / c - 1) * 100
    f['pair'] = pair
    return f


def build(period):
    cache = D.PERIODS[period]
    frames = [pf for pair in D.PAIRS if (pf := pair_frame(cache, pair)) is not None]
    allf = pd.concat(frames, ignore_index=True)
    allf = allf[(allf['t'] // H) % STEP_H == 0]
    btc = allf[allf.pair == 'BTCUSDT'].set_index('t')
    for h in (4, 24, 168):
        allf[f'btc_{h}'] = allf['t'].map(btc[f'ret_{h}'])
        allf[f'rel_{h}'] = allf[f'ret_{h}'] - allf[f'btc_{h}']
    allf['btc_vol24'] = allf['t'].map(btc['vol24'])
    grp = allf.groupby('t')
    allf['breadth24'] = grp['ret_24'].transform(lambda x: (x > 0).mean())
    allf['mkt_ret24'] = grp['ret_24'].transform('mean')
    if 'fund' in allf:
        allf['mkt_fund'] = grp['fund'].transform('mean')
    if 'oi_24' in allf:
        allf['mkt_oi24'] = grp['oi_24'].transform('mean')
    allf['rank_ret24'] = grp['ret_24'].rank(pct=True)
    allf['rank_ret168'] = grp['ret_168'].rank(pct=True)
    if 'fund' in allf:
        allf['rank_fund'] = grp['fund'].rank(pct=True)
    allf['period'] = period
    return allf.dropna(subset=['ret_720', 'fwd_24'])


FEATURES = None


def load_all():
    global FEATURES
    parts = []
    for period in D.PERIODS:
        path = os.path.join(HERE, 'results', f'ai_direction_ml_{period}.pkl')
        if not os.path.exists(path):
            build(period).to_pickle(path)
        parts.append(pd.read_pickle(path))
    data = pd.concat(parts, ignore_index=True)
    data['fold'] = data['period'].map(FOLD)
    data = data.drop_duplicates(subset=['pair', 't'], keep='first')
    drop = {'ts', 't', 'o', 'h', 'l', 'c', 'v', 'pair', 'period', 'fold'} | {f'fwd_{h}' for h in HORIZONS}
    FEATURES = [c for c in data.columns if c not in drop]
    return data


def fit_predict(train, test, target):
    """
    Гистограммный градиентный бустинг (scikit-learn): LightGBM 4.7 с numpy 2.5
    падает внутри библиотеки («access violation»), а это тот же алгоритм.
    """
    from sklearn.ensemble import HistGradientBoostingRegressor
    y = train[target].clip(train[target].quantile(0.01), train[target].quantile(0.99))
    model = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.03, max_leaf_nodes=15,
                                          min_samples_leaf=300, l2_regularization=5.0,
                                          random_state=7)
    x_train = train[FEATURES].to_numpy(dtype=np.float64)
    x_test = test[FEATURES].to_numpy(dtype=np.float64)
    model.fit(x_train, y.to_numpy(dtype=np.float64))
    return model.predict(x_test), model.predict(x_train), model


def evaluate(train, test, target, label):
    pred, pred_train, model = fit_predict(train, test, target)
    y = test[target].to_numpy()
    ic = pd.Series(pred).rank().corr(pd.Series(y).rank())
    hit = np.mean(np.sign(pred) == np.sign(y))
    cut = np.quantile(np.abs(pred_train), 0.8)
    strong = np.abs(pred) >= cut
    # Точки решения на одной паре — через 4 ч; на горизонте 24 ч позиции
    # перекрываются, поэтому сделки берутся не чаще раза в горизонт на пару.
    taken = []
    h = int(target.split('_')[1])
    last = {}
    for k in np.flatnonzero(strong):
        pair, t = test['pair'].iat[k], test['t'].iat[k]
        if last.get(pair, -1) > t:
            continue
        last[pair] = t + h * H
        taken.append(np.sign(pred[k]) * y[k] - COST_PCT)
    taken = np.array(taken)
    lo, hi = ci(taken) if len(taken) > 10 else (np.nan, np.nan)
    print(f'   {label:34s} IC {ic:+.3f}  знак {hit * 100:4.1f}%  сделок {len(taken):5d}  '
          f'чистыми {taken.mean() if len(taken) else 0:+.3f}% [{lo:+.3f}; {hi:+.3f}]', flush=True)
    return model, taken


def main():
    data = load_all()
    print(f'точек решения {len(data)}, признаков {len(FEATURES)}: {", ".join(FEATURES)}')
    for target in (f'fwd_{h}' for h in HORIZONS):
        print(f'\n== ГОРИЗОНТ {target.split("_")[1]} ч')
        print('  вперёд по времени (учится на прошлом, торгует следующее):')
        order = ['bear', 'mid1', 'mid2', 'recent']
        for k in (2, 3):
            train = data[data.fold.isin(order[:k])]
            test = data[data.fold == order[k]]
            evaluate(train, test, target, f'{"+".join(order[:k])} → {order[k]}')
        print('  «оставить один период»:')
        pooled = []
        model = None
        for fold in order:
            model, taken = evaluate(data[data.fold != fold], data[data.fold == fold], target, f'без {fold}')
            pooled.append(taken)
        allt = np.concatenate(pooled)
        lo, hi = ci(allt)
        print(f'   все периоды: {len(allt)} сделок, чистыми {allt.mean():+.3f}% [{lo:+.3f}; {hi:+.3f}] на сделку')
        from sklearn.inspection import permutation_importance
        probe = data[data.fold == order[-1]].sample(n=min(20000, int((data.fold == order[-1]).sum())), random_state=3)
        imp = permutation_importance(model, probe[FEATURES].to_numpy(dtype=np.float64),
                                     probe[target].to_numpy(dtype=np.float64), n_repeats=3, random_state=3)
        top = pd.Series(imp.importances_mean, index=FEATURES).sort_values(ascending=False)
        print('   важнее всего:', ', '.join(f'{k} {v:+.4f}' for k, v in top.iloc[:10].items()))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
