"""
Какие монеты будут СИЛЬНЕЕ остальных — вместо того, куда пойдёт рынок.

ЗАЧЕМ. research/ai_direction_ml.py показал: бустинг на всех данных биржи с
историей угадывает не направление, а режим своих учебных лет (+0.97% на
сделку в растущем 2024–25, −0.26% в падающем 2025–26). Общий ход рынка —
главный шум такой задачи. Здесь он вынут из цели: модель прогнозирует ход
монеты ОТНОСИТЕЛЬНО среднего по 20 парам в тот же момент, а торговля —
покупка трёх сильнейших прогнозов и продажа трёх слабейших одновременно, так
что общий ход в сделке почти гасится. Если в данных биржи есть устойчивое
знание о монетах, оно должно проявиться здесь в обоих режимах рынка.

Рядом — классические правила без модели (разворот за сутки, импульс за
неделю и месяц, фандинг, премия, ОИ): у каждого своё ранжирование, та же
торговля. Это нижняя планка — модель обязана её превзойти.

Проверка вперёд по времени (bear+mid1 → mid2; bear+mid1+mid2 → recent) и
«оставить один период». Точки решения: горизонт 24 ч — раз в сутки в 00:00
UTC, горизонт 4 ч — каждые 4 ч; позиции не перекрываются. Издержки на
позицию — круг тейкером 0.11% + проскальзывание 0.1% = 0.21%; фандинг
приближённо: последняя выплаченная ставка × число выплат за горизонт
(шорт получает положительный фандинг, лонг платит).

Данные — кэш ai_direction_ml (results/ai_direction_ml_*.pkl).

Запуск:
    python research/ai_xsection_ml.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_direction_ml as M                           # noqa: E402
from common import ci                                 # noqa: E402

H = M.H
COST_PCT = 0.21
TOP_K = 3
ORDER = ['bear', 'mid1', 'mid2', 'recent']
RANKED = ['ret_1', 'ret_4', 'ret_12', 'ret_24', 'ret_72', 'ret_168', 'ret_720', 'atr', 'vol24',
          'pos_24', 'pos_168', 'pos_720', 'ema20', 'ema50', 'ema200', 'ema50d', 'er30',
          'vol_ratio', 'vol_z1', 'wick_up', 'wick_dn', 'oi_4', 'oi_24', 'oi_168', 'oi_z',
          'fund', 'fund_z', 'prem', 'prem24', 'prem_z']
# Классические правила: (имя, признак, знак). Знак +1 — покупать высокие значения.
RULES = [
    ('разворот суток (купить отстающих)', 'ret_24', -1),
    ('разворот 3 суток', 'ret_72', -1),
    ('импульс недели', 'ret_168', +1),
    ('импульс месяца', 'ret_720', +1),
    ('фандинг (купить низкий)', 'fund', -1),
    ('премия (купить низкую)', 'prem24', -1),
    ('рост ОИ за сутки (купить растущий)', 'oi_24', +1),
    ('близость к месячному максимуму', 'pos_720', +1),
]


def add_week(data):
    """Ход за 168 ч вперёд по той же паре (строки кэша — каждые 4 ч)."""
    if 'fwd_168' in data:
        return data
    key = data['pair'] + ':' + data['t'].astype(str)
    close = pd.Series(data['c'].to_numpy(), index=key.to_numpy())
    ahead = (data['pair'] + ':' + (data['t'] + 168 * H).astype(str)).map(close)
    data['fwd_168'] = (ahead.to_numpy() / data['c'].to_numpy() - 1) * 100
    return data


def prepare(data, horizon, step=None, offset_h=0):
    target = f'fwd_{horizon}'
    step = step or (24 if horizon == 24 else 4)
    d = data[(data['t'] // H) % step == offset_h].copy()
    d = d.dropna(subset=[target])
    grp = d.groupby('t')
    d['xs'] = d[target] - grp[target].transform('mean')          # ход относительно рынка
    d['n_pairs'] = grp[target].transform('count')
    for col in RANKED:
        if col in d:
            d[f'r_{col}'] = grp[col].rank(pct=True)
    d = d[d['n_pairs'] >= 10]
    return d


def funding_pct(row_fund, side, horizon):
    """Фандинг за горизонт в процентах цены: лонг платит положительный, шорт получает."""
    if row_fund is None or np.isnan(row_fund):
        return 0.0
    return -side * row_fund / 100 * (horizon / 8)


def trade_book(frame, score, horizon, target):
    """Лонг TOP_K лучших по score, шорт TOP_K худших на каждой точке решения."""
    out, stamps = [], []
    fund = frame['fund'] if 'fund' in frame else pd.Series(np.nan, index=frame.index)
    tmp = pd.DataFrame({'t': frame['t'].to_numpy(), 's': np.asarray(score), 'y': frame[target].to_numpy(),
                        'f': fund.to_numpy(), 'mkt': frame[target].to_numpy() - frame['xs'].to_numpy()})
    tmp = tmp.dropna(subset=['s', 'y'])
    for t, g in tmp.groupby('t'):
        if len(g) < 2 * TOP_K:
            continue
        g = g.sort_values('s')
        longs, shorts = g.iloc[-TOP_K:], g.iloc[:TOP_K]
        legs = [(+1, r) for r in longs.itertuples()] + [(-1, r) for r in shorts.itertuples()]
        pnl = [side * r.y - COST_PCT + funding_pct(r.f, side, horizon) for side, r in legs]
        out.append((np.mean(pnl), g['mkt'].iat[0]))
        stamps.append(t)
    return np.array([p for p, _ in out]), np.array([m for _, m in out]), np.array(stamps)


def report(label, pnl, mkt):
    if len(pnl) < 10:
        print(f'   {label:42s} мало точек ({len(pnl)})')
        return
    lo, hi = ci(pnl)
    beta = np.corrcoef(pnl, mkt)[0, 1] if np.std(mkt) > 0 else np.nan
    print(f'   {label:42s} точек {len(pnl):5d}  на позицию {pnl.mean():+.3f}% [{lo:+.3f}; {hi:+.3f}]  '
          f'в плюс {np.mean(pnl > 0) * 100:4.1f}%  связь с рынком {beta:+.2f}', flush=True)


def fit(train, features, target='xs'):
    from sklearn.ensemble import HistGradientBoostingRegressor
    y = train[target].clip(train[target].quantile(0.01), train[target].quantile(0.99))
    model = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.03, max_leaf_nodes=15,
                                          min_samples_leaf=300, l2_regularization=5.0, random_state=7)
    model.fit(train[features].to_numpy(dtype=np.float64), y.to_numpy(dtype=np.float64))
    return model


def main():
    data = M.load_all()
    for horizon in (24, 4):
        target = f'fwd_{horizon}'
        d = prepare(data, horizon)
        ranks = [c for c in d.columns if c.startswith('r_')]
        raw = [c for c in M.FEATURES if c in d.columns]
        print(f'\n== ГОРИЗОНТ {horizon} ч: точек решения {d["t"].nunique()}, строк {len(d)}; '
              f'лонг {TOP_K} + шорт {TOP_K}, издержки {COST_PCT}% на позицию, с фандингом')
        print('  правила без модели (по периодам: bear | mid1 | mid2 | recent):')
        for name, col, sign in RULES:
            if col not in d:
                continue
            per = []
            for fold in ORDER:
                part = d[d.fold == fold]
                pnl, _, _ = trade_book(part, sign * part[col], horizon, target)
                per.append(pnl.mean() if len(pnl) else np.nan)
            pnl, mkt, _ = trade_book(d, sign * d[col], horizon, target)
            report(name, pnl, mkt)
            print(f'      по периодам: ' + ' | '.join(f'{x:+.3f}' for x in per))
        for fs_name, features in (('ранги (только сравнение монет)', ranks),
                                  ('ранги + сырые признаки', ranks + raw)):
            print(f'  модель, признаки: {fs_name} ({len(features)})')
            for k in (2, 3):
                train = d[d.fold.isin(ORDER[:k])]
                test = d[d.fold == ORDER[k]]
                model = fit(train, features)
                pnl, mkt, _ = trade_book(test, model.predict(test[features].to_numpy(dtype=np.float64)),
                                         horizon, target)
                report(f'{"+".join(ORDER[:k])} → {ORDER[k]}', pnl, mkt)
            pooled, pooled_mkt = [], []
            for fold in ORDER:
                model = fit(d[d.fold != fold], features)
                test = d[d.fold == fold]
                pnl, mkt, _ = trade_book(test, model.predict(test[features].to_numpy(dtype=np.float64)),
                                         horizon, target)
                report(f'без {fold}', pnl, mkt)
                pooled.append(pnl)
                pooled_mkt.append(mkt)
            report('все периоды («оставить один»)', np.concatenate(pooled), np.concatenate(pooled_mkt))


def main_week():
    """
    Удержание неделя: на ежедневной перестановке издержки (0.21% на позицию)
    больше, чем разница между монетами за сутки. Точка перестановки — раз в
    неделю; день недели не подбирается: считаются все семь сдвигов, итог —
    их среднее. Модель учится на ежедневных точках (цели перекрываются, это
    только обучение), торгует недельными; при проверке вперёд из обучения
    выброшены точки, чья неделя заходит в период проверки.
    """
    data = add_week(M.load_all())
    target = 'fwd_168'
    print(f'\n== ГОРИЗОНТ 168 ч (неделя): лонг {TOP_K} + шорт {TOP_K}, издержки {COST_PCT}% на позицию, '
          f'с фандингом; семь сдвигов дня перестановки')
    weeks = [prepare(data, 168, step=168, offset_h=24 * k) for k in range(7)]
    print('  правила без модели — среднее семи сдвигов (по периодам: bear | mid1 | mid2 | recent), '
          'разброс сдвигов:')
    for name, col, sign in RULES:
        if col not in data:
            continue
        per_offset, per_fold = [], {f: [] for f in ORDER}
        for w in weeks:
            pnl, _, _ = trade_book(w, sign * w[col], 168, target)
            per_offset.append(pnl.mean())
            for fold in ORDER:
                part = w[w.fold == fold]
                p, _, _ = trade_book(part, sign * part[col], 168, target)
                if len(p):
                    per_fold[fold].append(p.mean())
        pnl0, mkt0, _ = trade_book(weeks[0], sign * weeks[0][col], 168, target)
        lo, hi = ci(pnl0)
        print(f'   {name:42s} на позицию {np.mean(per_offset):+.3f}% (сдвиги {min(per_offset):+.3f}…'
              f'{max(per_offset):+.3f}; сдвиг 0: {len(pnl0)} недель [{lo:+.3f}; {hi:+.3f}])', flush=True)
        print('      по периодам: ' + ' | '.join(f'{np.mean(per_fold[f]):+.3f}' for f in ORDER))
    daily = prepare(data, 168, step=24)
    ranks = [c for c in daily.columns if c.startswith('r_')]
    raw = [c for c in M.FEATURES if c in daily.columns]
    for fs_name, features in (('ранги (только сравнение монет)', ranks), ('ранги + сырые признаки', ranks + raw)):
        print(f'  модель, признаки: {fs_name} ({len(features)}) — среднее семи сдвигов, в скобках разброс')
        for k in (2, 3):
            test_start = daily[daily.fold == ORDER[k]]['t'].min()
            train = daily[daily.fold.isin(ORDER[:k]) & (daily['t'] + 168 * H <= test_start)]
            model = fit(train, features)
            res = []
            for w in weeks:
                test = w[w.fold == ORDER[k]]
                pnl, _, _ = trade_book(test, model.predict(test[features].to_numpy(dtype=np.float64)), 168, target)
                res.append(pnl)
            means = [r.mean() for r in res]
            lo, hi = ci(res[0])
            print(f'   {"+".join(ORDER[:k])} → {ORDER[k]:8s} на позицию {np.mean(means):+.3f}% '
                  f'({min(means):+.3f}…{max(means):+.3f}); сдвиг 0: {len(res[0])} недель [{lo:+.3f}; {hi:+.3f}]',
                  flush=True)
        pooled = {k: [] for k in range(7)}
        for fold in ORDER:
            model = fit(daily[daily.fold != fold], features)
            for k, w in enumerate(weeks):
                test = w[w.fold == fold]
                pnl, _, _ = trade_book(test, model.predict(test[features].to_numpy(dtype=np.float64)), 168, target)
                pooled[k].append(pnl)
        means = [np.concatenate(v).mean() for v in pooled.values()]
        first = np.concatenate(pooled[0])
        lo, hi = ci(first)
        print(f'   «оставить один период»       на позицию {np.mean(means):+.3f}% ({min(means):+.3f}…'
              f'{max(means):+.3f}); сдвиг 0: {len(first)} недель [{lo:+.3f}; {hi:+.3f}]', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) > 1 and sys.argv[1] == '168':
        main_week()
    else:
        main()
