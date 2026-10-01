"""
Лента и стакан Binance на 15 мин – 4 ч: проверка на истории
(docs/Микроструктура_на_истории_2026-10-01.md, п. 85). Файлы бота не меняются.

Точка решения — каждые 15 минут по каждой из 30 пар тетради; признаки — только
по данным, известным к моменту решения; вход по открытию минуты t + 1 мин,
выход через 15 мин / 1 ч / 4 ч; издержки круга 0.17%.

    python research/micro_hist.py feat [ПАРЫ]   # → results/micro/feat_<ПАРА>.pkl
    python research/micro_hist.py eval          # правила + бустинг → печать (results/micro/eval_micro.txt)
"""
import bisect
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fetch_binance_klines_1m import EXTRA, POOL  # noqa: E402

OUT = os.path.join(HERE, 'results', 'micro')
KL = os.path.join(HERE, 'binance_klines_1m_cache')
BOOK = os.path.join(HERE, 'binance_bookdepth_cache')
MET = os.path.join(HERE, 'binance_metrics_cache')
PAIRS = POOL + EXTRA
COST = 0.0017
HORIZONS = (15, 60, 240)
MIN = pd.Timedelta(minutes=1)
MAX_GAP_MIN = 15
DAY15 = 96                                              # 15-минуток в сутках
WEEK15 = 672
WEEK5 = 2016

TAPE = ['dlt5', 'dlt15', 'dlt60', 'dlt240', 'cnt15r', 'size15r', 'vol15r']
PRICE = ['r15z', 'r60z', 'r240z', 'r1440z', 'vola', 'pos24']
BOOKF = ['imb1', 'imb2', 'dimb15', 'dimb60', 'dep1r']
POS = ['oi15z', 'oi60z', 'oi240z', 'lsacc']
MARKET = ['btc_r60z', 'btc_dlt60']
FEATURES = TAPE + PRICE + BOOKF + POS + MARKET
NO_BOOK = set(TAPE + PRICE + MARKET)                    # у этих есть 2021–22

TS = lambda s: pd.Timestamp(s, tz='UTC')                # noqa: E731
OLD = (TS('2021-01-01'), TS('2023-01-01'))
SEL = (TS('2023-01-01'), TS('2025-01-01'))
CHECK = (TS('2025-01-01'), TS('2026-10-01'))
HALF = TS('2025-11-16')
ML_TRAIN = (TS('2023-01-01'), TS('2024-07-01'))
ML_CUT = (TS('2024-07-01'), TS('2025-01-01'))


def _ns(index):
    return pd.DatetimeIndex(index).as_unit('ns')


def feat_path(pair):
    return os.path.join(OUT, f'feat_{pair}.pkl')


# ── Признаки ─────────────────────────────────────────────────────────────────
def _bad_days(isnan, grid):
    """Сутки, в которых есть дыра длиннее MAX_GAP_MIN минут подряд."""
    s = pd.Series(isnan, index=grid)
    run = s.groupby((~s).cumsum()).transform('sum')
    long_gap = s & (run > MAX_GAP_MIN)
    return set(grid[long_gap.to_numpy()].normalize())


def _five_min(path, grid_end):
    if not os.path.exists(path):
        return None
    f = pd.read_pickle(path).astype('float64')
    f.index = _ns(f.index)
    f = f[~f.index.duplicated(keep='last')].sort_index()
    g5 = pd.date_range(f.index[0].floor('5min'), grid_end, freq='5min')
    return f.groupby(f.index.floor('5min')).last().reindex(g5)


def build(pair):
    started = time.time()
    k = pd.read_pickle(os.path.join(KL, pair + '.pkl'))
    k.index = _ns(k.index)
    k = k[~k.index.duplicated(keep='last')].sort_index()
    grid = pd.date_range(k.index[0].ceil('D'), (k.index[-1] + MIN).floor('D'), freq='1min', inclusive='left')
    k = k.reindex(grid).astype('float64')
    # Простой Binance (обслуживание) выглядит как минуты без сделок — это тоже дыра.
    bad = _bad_days((k['c'].isna() | (k['n'] <= 0)).to_numpy(), grid)
    t_grid = grid[(grid.minute % 15) == 0]
    at = t_grid - MIN                                   # последняя закрытая минута к моменту t
    F = {}

    def win(s, n):
        return s.rolling(n, min_periods=int(n * 0.8)).sum()

    def on_t(s, lag_min=0):
        return s.reindex(at - lag_min * MIN).to_numpy()

    for n in (5, 15, 60, 240):
        sq, st = win(k['qv'], n), win(k['tbq'], n)
        F[f'dlt{n}'] = on_t((2 * st - sq) / sq)
    n15, q15 = on_t(win(k['n'], 15)), on_t(win(k['qv'], 15))

    def rel(x):
        x = pd.Series(x, index=t_grid)
        return (x / x.rolling(DAY15, min_periods=DAY15 // 2).median().shift(1)).to_numpy()

    F['cnt15r'] = rel(n15)
    F['size15r'] = rel(q15 / np.where(n15 > 0, n15, np.nan))
    F['vol15r'] = rel(q15)

    lp = np.log(k['c'])
    lr = lp.diff()
    sig = lr.rolling(1440, min_periods=1000).std()
    sig_t = on_t(sig)
    for n in (15, 60, 240, 1440):
        F[f'r{n}z'] = (on_t(lp) - on_t(lp, n)) / (sig_t * np.sqrt(n))
    F['vola'] = on_t(lr.rolling(60, min_periods=48).std()) / sig_t
    hi = k['h'].rolling(1440, min_periods=1000).max()
    lo = k['l'].rolling(1440, min_periods=1000).min()
    F['pos24'] = on_t((k['c'] - lo) / (hi - lo))

    for h in HORIZONS:
        entry = k['o'].reindex(t_grid + MIN).to_numpy()
        exit_ = k['o'].reindex(t_grid + (1 + h) * MIN).to_numpy()
        F[f'fwd{h}'] = exit_ / entry - 1

    def look(s, lag_min=0):
        """Снимок 5 мин с меткой ≤ t − 5 мин, не старше 15 мин."""
        return s.ffill(limit=3).reindex(t_grid - (5 + lag_min) * MIN).to_numpy()

    nan = np.full(len(t_grid), np.nan)
    b = _five_min(os.path.join(BOOK, pair + '.pkl'), grid[-1])
    if b is not None:
        imb1 = b['b1'] / (b['b1'] + b['a1'])
        dep = b['b1'] + b['a1']
        F['imb1'] = look(imb1)
        F['imb2'] = look(b['b2'] / (b['b2'] + b['a2']))
        F['dimb15'] = F['imb1'] - look(imb1, 15)
        F['dimb60'] = F['imb1'] - look(imb1, 60)
        F['dep1r'] = look(dep / dep.rolling(WEEK5, min_periods=WEEK5 // 2).median().shift(1))
    else:
        for c in BOOKF:
            F[c] = nan
    m = _five_min(os.path.join(MET, pair + '.pkl'), grid[-1])
    if m is not None:
        oi = np.log(m['sum_open_interest'].where(m['sum_open_interest'] > 0))
        for n in (15, 60, 240):
            d = pd.Series(look(oi) - look(oi, n), index=t_grid)
            F[f'oi{n}z'] = (d / d.rolling(WEEK15, min_periods=WEEK15 // 2).std().shift(1)).to_numpy()
        ls = np.log(m['count_long_short_ratio'].where(m['count_long_short_ratio'] > 0))
        F['lsacc'] = look(ls - ls.rolling(WEEK5, min_periods=WEEK5 // 2).median().shift(1))
    else:
        for c in POS:
            F[c] = nan

    out = pd.DataFrame(F, index=t_grid).astype(np.float32)
    keep = ~out.index.normalize().isin(list(bad))
    out = out[keep]
    out.to_pickle(feat_path(pair) + '.tmp')
    os.replace(feat_path(pair) + '.tmp', feat_path(pair))
    return (f'  {pair:13s} точек {len(out):7d} с {out.index[0]:%Y-%m-%d}, суток с дырой > {MAX_GAP_MIN} мин '
            f'{len(bad)}, стакан {"да" if b is not None else "нет"}, метрики {"да" if m is not None else "нет"}; '
            f'{time.time() - started:4.0f} с')


def _build_job(pair):
    sys.stdout.reconfigure(encoding='utf-8')
    return build(pair)


def features(pairs):
    os.makedirs(OUT, exist_ok=True)
    from multiprocessing import Pool
    with Pool(3) as pool:
        for line in pool.imap_unordered(_build_job, pairs):
            print(line, flush=True)


# ── Оценка ───────────────────────────────────────────────────────────────────
def load():
    parts = []
    for code, pair in enumerate(PAIRS):
        f = pd.read_pickle(feat_path(pair))
        f['pair'] = np.int16(code)
        parts.append(f)
    btc = parts[PAIRS.index('BTCUSDT')][['r60z', 'dlt60']].rename(columns={'r60z': 'btc_r60z',
                                                                          'dlt60': 'btc_dlt60'})
    data = pd.concat(parts)
    del parts
    data = data.join(btc, how='left')
    data['t'] = data.index
    data = data.sort_values(['pair', 't'], kind='stable').reset_index(drop=True)
    data['slot'] = (data['t'].astype('int64') // (15 * 60 * 10 ** 9)).astype(np.int64)
    data['day'] = (data['slot'] // DAY15).astype(np.int64)
    return data


class Book:
    """Сделки правила: сторона, горизонт, без перекрытия по паре; итоги по отрезкам."""

    def __init__(self, data):
        self.pair = data['pair'].to_numpy()
        self.slot = data['slot'].to_numpy()
        self.day = data['day'].to_numpy()
        self.t = data['t'].astype('int64').to_numpy()             # нс UTC
        self.fwd = {h: data[f'fwd{h}'].to_numpy(np.float64) for h in HORIZONS}
        bounds = np.flatnonzero(np.diff(self.pair)) + 1
        self.seg = list(zip(np.r_[0, bounds], np.r_[bounds, len(self.pair)]))

    def take(self, mask, h):
        """Номера сделок: правило сработало, цена выхода есть, позиция по паре не перекрывается."""
        sel = np.flatnonzero(mask & np.isfinite(self.fwd[h]))
        step = h // 15
        if step <= 1 or not len(sel):
            return sel
        taken = []
        p = self.pair[sel]
        cuts = np.flatnonzero(np.diff(p)) + 1
        for a, b in zip(np.r_[0, cuts], np.r_[cuts, len(sel)]):
            s = self.slot[sel[a:b]].tolist()
            i = 0
            while i < len(s):
                taken.append(a + i)
                i = bisect.bisect_left(s, s[i] + step, i + 1)
        return sel[np.asarray(taken, dtype=np.int64)]

    def stats(self, idx, side, h, span):
        t = self.t[idx]
        idx = idx[(t >= span[0].value) & (t < span[1].value)]
        r = side * self.fwd[h][idx] - COST
        n = len(r)
        weeks = (span[1] - span[0]).days / 7
        if n < 30:
            return {'n': n, 'mean': np.nan, 't': np.nan, 'per_week': n / weeks}
        mean = r.mean()
        _, day = np.unique(self.day[idx], return_inverse=True)
        s = np.bincount(day, weights=r)
        c = np.bincount(day)
        d = len(s)
        se = np.sqrt(((s - mean * c) ** 2).sum() * d / (d - 1)) / n
        return {'n': n, 'mean': mean, 't': mean / se if se > 0 else np.nan, 'per_week': n / weeks}


def _fmt(s):
    if not np.isfinite(s['mean']):
        return f'{s["n"]:6d} сд     —'
    return f'{s["n"]:6d} сд {100 * s["mean"]:+.3f}% t {s["t"]:+5.1f}'


def _in(data, span):
    return ((data['t'] >= span[0]) & (data['t'] < span[1])).to_numpy()


def _holm(rows, label):
    from scipy.stats import norm
    m = len(rows)
    print(f'\n  {label}: прошли отбор {m}')
    accepted = []
    rows = sorted(rows, key=lambda x: -x['check']['t'])
    for j, x in enumerate(rows):
        p = float(norm.sf(x['check']['t'])) if np.isfinite(x['check']['t']) else 1.0
        limit = 0.05 / (m - j)
        ok = (x['check']['mean'] > 0 and p < limit and x['half_a']['mean'] > 0 and x['half_b']['mean'] > 0
              and x['check']['per_week'] >= 2 and x.get('old_ok', True))
        print(f'    {x["name"]:34s} приёмка {_fmt(x["check"])} p {p:.4f} (порог {limit:.4f}) | половины '
              f'{100 * x["half_a"]["mean"]:+.3f}% / {100 * x["half_b"]["mean"]:+.3f}% | '
              f'{x["check"]["per_week"]:6.1f} сд/нед | 2021–22 {x.get("old_txt", "—")} → '
              f'{"ПРИНЯТО" if ok else "нет"}', flush=True)
        if ok:
            accepted.append(x['name'])
        if not (x['check']['mean'] > 0 and p < limit):
            break                                       # Холм: дальше пороги только строже
    return accepted


def evaluate():
    from scipy.stats import spearmanr
    started = time.time()
    data = load()
    bk = Book(data)
    in_sel, in_chk = _in(data, SEL), _in(data, CHECK)
    print(f'точек решения {len(data)}, пар {data["pair"].nunique()}; '
          f'отбор {in_sel.sum()}, приёмка {in_chk.sum()}; загрузка {time.time() - started:.0f} с')
    print('\nПОКРЫТИЕ признаков (доля точек с значением): 2021–22 | отбор | приёмка')
    for c in FEATURES:
        cov = [100 * np.isfinite(data[c].to_numpy()[_in(data, s)]).mean() for s in (OLD, SEL, CHECK)]
        print(f'  {c:9s} ' + ' | '.join(f'{x:5.1f}%' for x in cov))

    print('\nБАЗА — каждая точка без условий (после 0.17%, без перекрытия по паре)')
    always = np.ones(len(data), bool)
    for h in HORIZONS:
        idx = bk.take(always, h)
        for side, name in ((1, 'лонг'), (-1, 'шорт')):
            print(f'  {h:3d} мин {name}: ' + ' | '.join(
                f'{lab} {_fmt(bk.stats(idx, side, h, span))}'
                for lab, span in (('2021–22', OLD), ('отбор', SEL), ('приёмка', CHECK))))

    print('\nРАНГОВАЯ СВЯЗЬ признака с доходом за H (до издержек): отбор / приёмка')
    for c in FEATURES:
        cells = []
        for h in HORIZONS:
            vals = []
            for m in (in_sel, in_chk):
                x, y = data[c].to_numpy()[m], data[f'fwd{h}'].to_numpy()[m]
                ok = np.isfinite(x) & np.isfinite(y)
                vals.append(spearmanr(x[ok][::7], y[ok][::7]).statistic if ok.sum() > 1000 else np.nan)
            cells.append(f'{h:3d}м {vals[0]:+.3f}/{vals[1]:+.3f}')
        print(f'  {c:9s} ' + '  '.join(cells), flush=True)

    edges = {}
    print('\nДОХОД ПО ПЯТЫМ ДОЛЯМ (до издержек, б. п. = 0.01%; границы — по отбору): отбор || приёмка')
    for c in FEATURES:
        x_sel = data[c].to_numpy()[in_sel]
        edges[c] = np.nanquantile(x_sel, [0.2, 0.4, 0.6, 0.8])
        for h in HORIZONS:
            cells = []
            for m in (in_sel, in_chk):
                x, y = data[c].to_numpy()[m], data[f'fwd{h}'].to_numpy()[m]
                ok = np.isfinite(x) & np.isfinite(y)
                q = np.searchsorted(edges[c], x[ok])
                cells.append(' '.join(f'{1e4 * y[ok][q == j].mean():+6.1f}' for j in range(5)))
            print(f'  {c:9s} {h:3d}м  {cells[0]}  ||  {cells[1]}')

    print('\nПРАВИЛА (288): крайняя пятая доля признака → сторона, горизонт; после 0.17%')
    passed = []
    for c in FEATURES:
        x = data[c].to_numpy()
        for q_name, mask in (('Q1', x <= edges[c][0]), ('Q5', x >= edges[c][3])):
            for h in HORIZONS:
                idx = bk.take(mask, h)
                for side, s_name in ((1, 'лонг'), (-1, 'шорт')):
                    name = f'{c} {q_name} {s_name} {h}м'
                    sel = bk.stats(idx, side, h, SEL)
                    if not (sel['mean'] > 0 and sel['t'] >= 2):
                        continue
                    row = {'name': name, 'sel': sel, 'check': bk.stats(idx, side, h, CHECK),
                           'half_a': bk.stats(idx, side, h, (CHECK[0], HALF)),
                           'half_b': bk.stats(idx, side, h, (HALF, CHECK[1]))}
                    if c in NO_BOOK:
                        old = bk.stats(idx, side, h, OLD)
                        row['old_ok'] = bool(old['mean'] > 0)
                        row['old_txt'] = f'{100 * old["mean"]:+.3f}%'
                    print(f'  отбор прошло: {name:34s} {_fmt(sel)} | приёмка {_fmt(row["check"])}', flush=True)
                    passed.append(row)
    accepted = _holm(passed, 'ПРАВИЛА')

    print('\nБУСТИНГ: учится на 2023-01…2024-06, порог — 2024-07…12, приёмка 2025-01…2026-09')
    from sklearn.ensemble import HistGradientBoostingRegressor
    tr, cut = _in(data, ML_TRAIN), _in(data, ML_CUT)
    X = data[FEATURES].to_numpy(np.float32)
    ml_passed = []
    for h in HORIZONS:
        y = data[f'fwd{h}'].to_numpy(np.float64)
        ok = tr & np.isfinite(y)
        yt = np.log1p(y[ok])
        yt = np.clip(yt, *np.quantile(yt, [0.01, 0.99]))
        t0 = time.time()
        model = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.03, max_leaf_nodes=15,
                                              min_samples_leaf=300, l2_regularization=5.0, random_state=7)
        model.fit(X[ok], yt)
        pred = np.full(len(data), np.nan)
        rest = ~tr
        pred[rest] = model.predict(X[rest])
        lo, hi = np.nanquantile(pred[cut], [0.2, 0.8])
        print(f'  {h:3d} мин: обучено на {ok.sum()} точках за {time.time() - t0:.0f} с, '
              f'ранговая связь прогноза с доходом: порог-отрезок '
              f'{spearmanr(pred[cut & np.isfinite(y)], y[cut & np.isfinite(y)]).statistic:+.3f}, '
              f'приёмка {spearmanr(pred[in_chk & np.isfinite(y)], y[in_chk & np.isfinite(y)]).statistic:+.3f}',
              flush=True)
        for side, s_name, mask in ((1, 'лонг', pred >= hi), (-1, 'шорт', pred <= lo)):
            idx = bk.take(mask, h)
            name = f'бустинг {s_name} {h}м'
            row = {'name': name, 'sel': bk.stats(idx, side, h, ML_CUT), 'check': bk.stats(idx, side, h, CHECK),
                   'half_a': bk.stats(idx, side, h, (CHECK[0], HALF)),
                   'half_b': bk.stats(idx, side, h, (HALF, CHECK[1]))}
            print(f'    {name:20s} порог-отрезок {_fmt(row["sel"])} | приёмка {_fmt(row["check"])} | '
                  f'половины {100 * row["half_a"]["mean"]:+.3f}% / {100 * row["half_b"]["mean"]:+.3f}%', flush=True)
            if row['sel']['mean'] > 0 and row['sel']['t'] >= 2:
                ml_passed.append(row)
    accepted += _holm(ml_passed, 'БУСТИНГ')
    print(f'\nИТОГ: принято {accepted or "ничего"}; {time.time() - started:.0f} с')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1:2]
    if cmd == ['feat']:
        features(sys.argv[2:] or list(PAIRS))
    elif cmd == ['eval']:
        evaluate()
