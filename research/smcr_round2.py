"""
Разбор SMC, второй круг (01.10.2026, docs/SMC_разбор_аналитика_2026-10-01.md,
раздел 7): R1–R5 и R7 на заявках «глазами бота» (results/smcr/live_orders.pkl,
research/smcr_live.py build). R6 — research/smcr_struct.py B_htf B_any.

    python research/smcr_round2.py features   # → results/smcr/live_orders_r2.pkl
    python research/smcr_round2.py eval       # → печать (results/smcr/eval_round2.txt)
"""
import os
import sys
from functools import lru_cache

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smcr_live as L                                 # noqa: E402  (D.PERIODS['fresh'] → fresh20)
import smcr_eval as V                                 # noqa: E402
from common import ci, diff_ci                         # noqa: E402

E = L.E
OUT = L.OUT
H_MS = 3_600_000
SRC = os.path.join(HERE, 'flow_cache_src')
LAYOUT = {'pool': 'live', 'other': 'live20', 'x12': 'live12x'}


# ── Данные ───────────────────────────────────────────────────────────────────
def _ms_index(df):
    return ((df.index - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy('int64')


@lru_cache(maxsize=None)
def spot(pair):
    p = os.path.join(SRC, 'spot', pair + '.pkl')
    if not os.path.exists(p):
        return None
    df = pd.read_pickle(p)
    return _ms_index(df), df['spot_qv'].to_numpy(float), df['spot_c'].to_numpy(float)


@lru_cache(maxsize=None)
def coinbase(pair):
    p = os.path.join(SRC, 'cb', pair + '.pkl')
    if not os.path.exists(p):
        return None
    df = pd.read_pickle(p)
    return _ms_index(df), df['cb_c'].to_numpy(float)


@lru_cache(maxsize=None)
def dvol():
    df = pd.read_pickle(os.path.join(SRC, 'dvol', 'BTC.pkl'))
    return _ms_index(df), df['dvol'].to_numpy(float)


@lru_cache(maxsize=None)
def perp_qv(pair):
    fl = L.flow(pair)
    if fl is None:
        return None
    parts = []
    for f in ('flow_cache_2021', 'flow_cache', 'flow_cache_new', 'flow_cache_extra'):
        p = os.path.join(HERE, f, pair + '.pkl')
        if os.path.exists(p):
            parts.append(pd.read_pickle(p)[['qv']])
    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep='last')].sort_index()
    return _ms_index(df), df['qv'].to_numpy(float)


@lru_cache(maxsize=None)
def funding(period, pair):
    got = L.E.S.load_series(L.D.PERIODS[period], 'funding', pair, 'funding_rate')
    return got


def _sum_between(ms, vals, a_ms, b_ms):
    """Сумма часовых значений с меткой открытия в [a_ms, b_ms]."""
    a = int(np.searchsorted(ms, a_ms, side='left'))
    b = int(np.searchsorted(ms, b_ms, side='right'))
    if b <= a:
        return np.nan
    v = vals[a:b]
    return np.nansum(v) if np.isfinite(v).any() else np.nan


def _value_at(ms, vals, t_ms, max_age_h=2):
    """Последнее значение не старше max_age_h часов — иначе прочерк (CLAUDE.md,
    «Данные»): у XRP на Coinbase дыра 2021–2023, и последнее известное
    значение давало «премию» в тысячи б.п."""
    k = int(np.searchsorted(ms, t_ms, side='right')) - 1
    if k < 0 or t_ms - ms[k] > max_age_h * H_MS:
        return np.nan
    return vals[k]


# ── Признаки второго круга ───────────────────────────────────────────────────
def features_for(rec, row):
    out = {}
    side = rec['dir']
    t = int(rec['t'])                     # закрытие бара решения
    t_open = t - H_MS                     # открытие бара решения
    # R1: толпа по трём последним выплатам (как фильтр бота, но средняя)
    fr = funding(rec['period'], rec['pair'])
    if fr is not None:
        ts, vals = fr
        k = int(np.searchsorted(ts, t, side='right')) - 1
        if k >= 2:
            out['fund_last'] = vals[k] * 1e4 * side
            out['fund_avg3'] = float(np.mean(vals[k - 2:k + 1])) * 1e4 * side
    # R2: доля спота в обороте за откат (от бара после конца ноги до бара решения)
    c1h = L.candles_1h(rec['period'], rec['pair'])
    sp, pq = spot(rec['pair']), perp_qv(rec['pair'])
    if c1h is not None and sp is not None and pq is not None:
        leg_end = int(row.leg_end_i)
        if 0 <= leg_end < len(c1h):
            a_ms = int(pd.Timestamp(c1h['timestamp'].iloc[leg_end]).value // 10 ** 6) + H_MS
            if a_ms <= t_open:
                s_w = _sum_between(sp[0], sp[1], a_ms, t_open)
                p_w = _sum_between(pq[0], pq[1], a_ms, t_open)
                s_b = _sum_between(sp[0], sp[1], a_ms - 720 * H_MS, a_ms - H_MS)
                p_b = _sum_between(pq[0], pq[1], a_ms - 720 * H_MS, a_ms - H_MS)
                if all(np.isfinite(x) and x > 0 for x in (s_w, p_w, s_b, p_b)):
                    out['spot_rel'] = (s_w / (s_w + p_w)) / (s_b / (s_b + p_b))
    # R3: премия Coinbase к споту Binance, б.п., среднее за 4 ч, в сторону сделки
    cb = coinbase(rec['pair'])
    if cb is not None and sp is not None:
        prem = []
        for h in range(4):
            tt = t_open - h * H_MS
            c = _value_at(cb[0], cb[1], tt)
            s = _value_at(sp[0], sp[2], tt)
            if np.isfinite(c) and np.isfinite(s) and s > 0:
                prem.append((c / s - 1) * 1e4)
        if len(prem) == 4:
            out['cb_prem'] = side * float(np.mean(prem))
    # R4: DVOL за сутки, %
    dv = dvol()
    now = _value_at(dv[0], dv[1], t_open)
    before = _value_at(dv[0], dv[1], t_open - 24 * H_MS)
    if np.isfinite(now) and np.isfinite(before) and before > 0:
        out['dvol_chg'] = (now / before - 1) * 100
    # R5: возраст блока на момент заявки, часов
    out['age_h'] = int(row.i) - int(row.poi_index)
    return out


def build_features():
    f = pd.read_pickle(os.path.join(OUT, 'live_orders.pkl'))
    base = f[f['sample'].isin(LAYOUT)].copy()
    lookup = {}
    for (sample, period), _g in base.groupby(['sample', 'period']):
        rows = E.rows(period, LAYOUT[sample])
        for r in rows.itertuples():
            lookup[(sample, period, r.pair, int(r.t), int(r.dir))] = r
    feats = []
    miss = 0
    for rec in base.to_dict('records'):
        row = lookup.get((rec['sample'], rec['period'], rec['pair'], int(rec['t']), int(rec['dir'])))
        if row is None:
            miss += 1
            feats.append({})
            continue
        feats.append(features_for(rec, row))
    extra = pd.DataFrame(feats, index=base.index)
    out = pd.concat([base, extra], axis=1)
    path = os.path.join(OUT, 'live_orders_r2.pkl')
    out.to_pickle(path)
    print(f'→ {path}: {len(out)} заявок, без строки стенда {miss}')
    print(out[['fund_last', 'fund_avg3', 'spot_rel', 'cb_prem', 'dvol_chg', 'age_h']].describe().T.round(3))


# ── Оценка ───────────────────────────────────────────────────────────────────
FILTERS2 = {
    'R2 откат на плече': ('spot_rel', lambda x: x < 1.0),
    'R3 спрос США не против': ('cb_prem', lambda x: x >= 0.0),
    'R4 без паники (DVOL не вырос)': ('dvol_chg', lambda x: x <= 0.0),
    'R5 зона не старше 24 ч': ('age_h', lambda x: x <= 24),
}


def _mask(f, name):
    col, rule = FILTERS2[name]
    have = f[col].notna().to_numpy()
    m = np.array([bool(rule(v)) if h else False for v, h in zip(f[col], have)])
    return m, have


def filters2(pool, others, label):
    print(f'\nФИЛЬТРЫ R2–R5 — {label}')
    early, late = pool[V.early_mask(pool)].reset_index(drop=True), pool[V.late_mask(pool)].reset_index(drop=True)
    cands = []
    for name in FILTERS2:
        m, have = _mask(early, name)
        eff, p, n = V.placebo(early[have].reset_index(drop=True), m[have])
        keep = np.isfinite(eff) and eff > 0 and p < 0.10
        print(f'  {name:32s} отбор: {n:4d} из {have.sum():4d}, эффект {eff:+.3f}, p {p:.3f}'
              f'  {"→ кандидат" if keep else ""}', flush=True)
        if keep:
            cands.append(name)
    res = []
    for name in cands:
        m, have = _mask(late, name)
        part = late[have].reset_index(drop=True)
        eff, p, n = V.placebo(part, m[have])
        eff_ft, _, _ = V.placebo(part, m[have], col='r_ft')
        ind = []
        for lab, o in others.items():
            o = o.reset_index(drop=True)
            mo, ho = _mask(o, name)
            e_o, p_o, n_o = V.placebo(o[ho].reset_index(drop=True), mo[ho])
            ind.append((lab, e_o, p_o, n_o))
        res.append((p, name, eff, n, eff_ft, ind))
    res.sort(key=lambda x: np.nan_to_num(x[0], nan=1.0))
    k = len(res)
    for i, (p, name, eff, n, eff_ft, ind) in enumerate(res):
        limit = 0.05 / (k - i)
        ok = eff > 0 and p < limit and eff_ft > 0 and all(e > 0 for _, e, _, _ in ind if np.isfinite(e))
        print(f'  {name:32s} приёмка: {n:4d} сд, эффект {eff:+.3f}, p {p:.4f} (порог {limit:.4f}), '
              f'насквозь {eff_ft:+.3f}; ' + '; '.join(f'{lab}: {n_o} сд {e:+.3f} p {pp:.3f}' for lab, e, pp, n_o in ind)
              + f'  {"ПРИНЯТ" if ok else "нет"}', flush=True)


def _tot(x, col='r'):
    r = x[col].dropna().to_numpy(float)
    return len(r), (r.mean() if len(r) else np.nan), r.sum()


def r1_crowd(pool, other):
    print('\nR1 СГЛАЖЕННАЯ ТОЛПА — замена фильтра: средняя трёх выплат ≤ −1 б.п. против последней')
    for lab, f in (('10 пар пула', pool), ('10 пар вне пула', other)):
        f = f[f['fund_last'].notna()]
        old = f[f['fund_last'] <= -1.0 + 1e-9]
        new = f[f['fund_avg3'] <= -1.0 + 1e-9]
        cells = []
        for p in V.PERIODS5:
            no, mo, so = _tot(old[old['period'] == p])
            nn, mn, sn = _tot(new[new['period'] == p])
            cells.append(f'{p} {so:+6.1f}→{sn:+6.1f} ({no}→{nn})')
        lo_, ln_ = old[V.late_mask(old)], new[V.late_mask(new)]
        a, b = lo_['r'].dropna().to_numpy(float), ln_['r'].dropna().to_numpy(float)
        dlo, dhi = diff_ci(b, a) if len(a) > 5 and len(b) > 5 else (np.nan, np.nan)
        print(f'  {lab}: ' + ' | '.join(cells))
        print(f'     приёмка: прежний {fmt_r(a)}; сглаженный {fmt_r(b)}; разница средних [{dlo:+.3f}; {dhi:+.3f}]; '
              f'насквозь {_tot(lo_, "r_ft")[2]:+.1f} → {_tot(ln_, "r_ft")[2]:+.1f}R')
        if lab == '10 пар пула':
            early_ok = all(_tot(new[new['period'] == p])[2] > _tot(old[old['period'] == p])[2] for p in V.EARLY)
            print(f'     отбор (bear и mid1 оба лучше): {"да" if early_ok else "нет"}')


def fmt_r(r):
    r = np.asarray(r, float)
    if len(r) < 3:
        return f'{len(r)} сд'
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    return f'{len(r)} сд {r.mean():+.3f} [{lo:+.3f}; {hi:+.3f}] итог {r.sum():+.1f}R'


ML_FEATURES = ['dir', 'conf', 'rr', 'stop_pct', 'fund_last', 'fund_avg3', 'F1', 'F2', 'F3', 'D3',
               'spot_rel', 'cb_prem', 'dvol_chg', 'age_h', 'swept', 'ote', 'bos']


def r7_model(pool, other, x12):
    from sklearn.ensemble import HistGradientBoostingRegressor
    print('\nR7 МОДЕЛЬ ОТБОРА — бустинг на 2022–24 (10 пар пула), верхняя треть прогноза')
    def X(f):
        g = f.copy()
        g['bos'] = (g['brk'] == 'BOS').astype(float)
        g['swept'] = g['swept'].astype(float)
        g['ote'] = g['ote'].astype(float)
        return g[ML_FEATURES].astype(float).to_numpy()
    train = pool[V.early_mask(pool) & pool['r'].notna()]
    model = HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=200,
                                          min_samples_leaf=40, l2_regularization=1.0, random_state=1)
    model.fit(X(train), train['r'].to_numpy(float))
    cut = float(np.quantile(model.predict(X(train)), 2 / 3))
    print(f'  обучение: {len(train)} сделок; порог верхней трети по обучению {cut:+.3f}')
    for lab, f in (('приёмка, 10 пар пула', pool[V.late_mask(pool)]),
                   ('10 пар вне пула, все периоды', other), ('23 пары 12m', x12),
                   ('приёмка, живая SMC (фильтр толпы)', pool[V.late_mask(pool) & pool['crowd']])):
        f = f.reset_index(drop=True)
        if len(f) < 10:
            continue
        pred = model.predict(X(f))
        m = pred >= cut
        eff, p, n = V.placebo(f, m)
        eff_ft, _, _ = V.placebo(f, m, col='r_ft')
        sel = f.loc[m, 'r'].dropna().to_numpy(float)
        print(f'  {lab:36s}: выбрано {n} сд, эффект {eff:+.3f}, p {p:.3f}, насквозь {eff_ft:+.3f}; '
              f'выбранные {fmt_r(sel)}')


def evaluate():
    f = pd.read_pickle(os.path.join(OUT, 'live_orders_r2.pkl'))
    pool = f[f['sample'] == 'pool'].reset_index(drop=True)
    other = f[f['sample'] == 'other'].reset_index(drop=True)
    x12 = f[f['sample'] == 'x12'].reset_index(drop=True)
    print('Покрытие признаков (доля заявок пула):',
          ', '.join(f'{c} {pool[c].notna().mean():.0%}' for c in ('fund_avg3', 'spot_rel', 'cb_prem', 'dvol_chg', 'age_h')))
    for label, sel, use_x in (('ядро без фильтра толпы', lambda d: d, True),
                              ('живая SMC с фильтром толпы', lambda d: d[d['crowd'] & d['fund_bp'].notna()], False)):
        P, O, X_ = sel(pool).reset_index(drop=True), sel(other).reset_index(drop=True), sel(x12).reset_index(drop=True)
        others = {'10 пар вне пула': O, '23 пары 12m': X_} if use_x else {'10 пар вне пула': O}
        filters2(P, others, label)
    r1_crowd(pool, other)
    r7_model(pool, other, x12)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['features']:
        build_features()
    elif sys.argv[1:2] == ['eval']:
        evaluate()
