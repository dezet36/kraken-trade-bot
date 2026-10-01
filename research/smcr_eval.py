"""
Разбор SMC 01.10.2026: оценка по протоколу docs/SMC_разбор_аналитика_2026-10-01.md.

    python research/smcr_eval.py live     # картина, D1–D4, F1–F4 (после smcr_live.py build)
    python research/smcr_eval.py struct   # S1–S4 (после smcr_struct.py)
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import ci, diff_ci                         # noqa: E402

OUT = os.path.join(HERE, 'results', 'smcr')
RNG = np.random.default_rng(20261001)
N_PERM = 20000
EARLY = ('bear', 'mid1')
END_12M = int(pd.Timestamp('2026-07-04 23:00', tz='UTC').value // 10 ** 6)


def late_mask(f):
    """mid2 + 12m + хвост fresh после конца 12m — без двойного счёта (А1)."""
    return (f['period'].isin(['mid2', '12m'])) | ((f['period'] == 'fresh') & (f['t'] > END_12M))


def early_mask(f):
    return f['period'].isin(EARLY)


def z(x):
    return 0.0 if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)


def fmt(r):
    r = np.asarray([x for x in r if x is not None and np.isfinite(x)], float)
    if len(r) < 3:
        return f'{len(r):4d} сд'
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    return f'{len(r):4d} сд, в плюс {np.mean(r > 0):4.0%}, {r.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}], итог {r.sum():+7.1f}R'


def placebo(frame, mask, col='r'):
    """Эффект фильтра к среднему R сделок и p: случайный отбор того же размера
    внутри (период × сторона)."""
    ok = frame[col].notna().to_numpy()
    part = frame[ok].reset_index(drop=True)
    m = np.asarray(mask)[ok]
    r = part[col].to_numpy(dtype=float)
    if m.sum() < 5 or m.sum() == len(m):
        return np.nan, np.nan, int(m.sum())
    effect = r[m].mean() - r.mean()
    groups = [np.asarray(g) for g in part.groupby(['period', 'dir']).indices.values()]
    ks = [int(m[g].sum()) for g in groups]
    sims = np.empty(N_PERM)
    for i in range(N_PERM):
        tot = cnt = 0
        for g, k in zip(groups, ks):
            if k:
                pick = RNG.choice(g, size=k, replace=False)
                tot += r[pick].sum()
                cnt += k
        sims[i] = tot / cnt - r.mean()
    p = (np.sum(sims >= effect) + 1) / (N_PERM + 1)
    return effect, p, int(m.sum())


# ── Картина ──────────────────────────────────────────────────────────────────
def picture(f, label):
    filled = f[f['r'].notna()]
    r = filled['r'].to_numpy(float)
    print(f'\n{label}: заявок {len(f)}, налилось {len(filled)} ({len(filled) / max(1, len(f)):.0%})')
    print(f'  R на сделку: {fmt(r)}')
    print(f'  валовой {filled["gross_r"].mean():+.3f}R, комиссии {filled["fees_r"].mean():.3f}R, '
          f'фандинг плоский {filled["flat_fund_r"].mean():.3f}R; с фандингом по факту '
          f'{(filled["r"] + filled["f4_adj"]).mean():+.3f}R')
    ex = filled['exit'].fillna('?').str.replace(r'SL_after_TP\d', 'SL после цели', regex=True)
    print('  выходы: ' + ', '.join(f'{k} {v:.0%}' for k, v in ex.value_counts(normalize=True).items()))
    print(f'  стоп медиана {f["stop_pct"].median():.2f}%, R:R медиана {f["rr"].median():.1f}; '
          f'ожидание налива медиана {filled["wait_h"].median():.1f} ч, в сделке {filled["hold_h"].median():.1f} ч')
    los = filled[filled['r'] < 0]
    win = filled[filled['r'] > 0]
    print(f'  проигравшие: до +0.5R доходили {np.mean(los["mfe"] >= 0.5):.0%}, до +1R {np.mean(los["mfe"] >= 1):.0%}; '
          f'выигравшие сначала уходили на −0.5R: {np.mean(win["mae"] >= 0.5):.0%}')
    print(f'  стоп поставлен по снятию: {f["stop_by_sweep"].mean():.0%} заявок; из них снятие ДО начала ноги '
          f'(не связано с блоком): {f.loc[f["stop_by_sweep"], "sweep_unrelated"].mean():.0%}')
    by = filled.groupby('period')['r'].agg(['size', 'mean', 'sum'])
    print('  по периодам: ' + '; '.join(f'{p} {int(n)} сд {m:+.3f} ({s:+.1f}R)' for p, (n, m, s) in by.iterrows()))
    side = filled.groupby('dir')['r'].agg(['size', 'mean', 'sum'])
    print('  стороны: ' + '; '.join(f'{"лонг" if d > 0 else "шорт"} {int(n)} сд {m:+.3f} ({s:+.1f}R)'
                                    for d, (n, m, s) in side.iterrows()))


# ── Фильтры ──────────────────────────────────────────────────────────────────
FILTERS = {
    'D3 старший дисконт': ('D3', lambda x: x <= 0.5),
    'D4 блок от BOS': ('brk', lambda x: x == 'BOS'),
    'F1 импульс на агрессии': ('F1', lambda x: x > 0),
    'F2 импульс на новых позициях': ('F2', lambda x: x > 0),
    'F3 откат без встречного набора': ('F3', lambda x: x < 0),
}


def mask_of(f, name):
    col, rule = FILTERS[name]
    vals = f[col]
    have = vals.notna() if col != 'brk' else pd.Series(True, index=f.index)
    m = np.array([bool(rule(v)) if h else False for v, h in zip(vals, have)])
    return m, have.to_numpy()


def filters(pool, others, base_label):
    print(f'\nФИЛЬТРЫ — {base_label} (эффект к среднему R сделок; p — плацебо внутри период × сторона)')
    early = pool[early_mask(pool)]
    late = pool[late_mask(pool)]
    cands = []
    for name in FILTERS:
        m, have = mask_of(early, name)
        part = early[have]
        eff, p, n = placebo(part, m[have])
        keep = np.isfinite(eff) and eff > 0 and p < 0.10
        print(f'  {name:32s} отбор: {n:4d} из {have.sum():4d}, эффект {eff:+.3f}, p {p:.3f}'
              f'  {"→ кандидат" if keep else ""}', flush=True)
        if keep or name.startswith('D4'):
            cands.append(name)
    res = []
    for name in cands:
        m, have = mask_of(late, name)
        part = late[have]
        eff, p, n = placebo(part, m[have])
        eff_ft, _, _ = placebo(part, m[have], col='r_ft')
        ind = []
        for lab, o in others.items():
            mo, ho = mask_of(o, name)
            e_o, p_o, n_o = placebo(o[ho], mo[ho])
            ind.append((lab, e_o, p_o, n_o))
        res.append((p, name, eff, n, eff_ft, ind))
    res.sort(key=lambda x: (np.nan_to_num(x[0], nan=1.0)))
    k = len(res)
    for i, (p, name, eff, n, eff_ft, ind) in enumerate(res):
        limit = 0.05 / (k - i)
        ok = eff > 0 and p < limit and eff_ft > 0 and all(e > 0 for _, e, _, _ in ind if np.isfinite(e))
        tag = '(после взгляда — только независимые)' if name.startswith('D4') else ''
        print(f'  {name:32s} приёмка: {n:4d} сд, эффект {eff:+.3f}, p {p:.4f} (порог {limit:.4f}), насквозь {eff_ft:+.3f}; '
              + '; '.join(f'{lab}: {n_o} сд {e:+.3f} p {pp:.3f}' for lab, e, pp, n_o in ind)
              + f'  {"ПРИНЯТ" if ok else "нет"} {tag}', flush=True)


# ── Исполнение ───────────────────────────────────────────────────────────────
def paired(f, a, b):
    d = np.array([z(x) - z(y) for x, y in zip(f[a], f[b])])
    if len(d) < 10:
        return np.nan, (np.nan, np.nan), len(d)
    boot = RNG.choice(d, size=(5000, len(d)), replace=True).mean(axis=1)
    return d.mean(), tuple(np.percentile(boot, [2.5, 97.5])), len(d)


def execution(pool, others, d2, d2_others, base_label):
    print(f'\nИСПОЛНЕНИЕ — {base_label}')
    early, late = pool[early_mask(pool)], pool[late_mask(pool)]
    # D1: парно, на заявку (неналитая = 0R)
    for lab, part in (('отбор', early), ('приёмка', late)):
        d, (lo, hi), n = paired(part, 'r_d1', 'r')
        d_ft, _, _ = paired(part, 'r_d1_ft', 'r_ft')
        canc = (part['r'].notna() & part['r_d1'].isna()).sum()
        print(f'  D1 снять заявку при потере направления, {lab}: {n} заявок, снято наливавшихся {canc}, '
              f'разница {d:+.4f}R/заявку [{lo:+.4f}; {hi:+.4f}], насквозь {d_ft:+.4f}')
    for lab, o in others.items():
        d, (lo, hi), n = paired(o, 'r_d1', 'r')
        print(f'  D1 {lab}: {n} заявок, разница {d:+.4f} [{lo:+.4f}; {hi:+.4f}]')
    # снятые: чем бы кончились
    gone = pool[pool['r'].notna() & pool['r_d1'].isna()]
    if len(gone):
        print(f'  D1: сделки, которые снятие убирает: {fmt(gone["r"])}')
    # D2: свои наборы
    for lab, mask in (('отбор', early_mask), ('приёмка', late_mask)):
        a = pool[mask(pool)]['r'].dropna().to_numpy(float)
        b = d2[mask(d2)]['r'].dropna().to_numpy(float)
        lo, hi = diff_ci(b, a) if len(a) > 5 and len(b) > 5 else (np.nan, np.nan)
        print(f'  D2 стоп за началом ноги, {lab}: база {fmt(a)}\n'
              f'  {"":24s}вариант {fmt(b)}; разница средних [{lo:+.3f}; {hi:+.3f}]')
    for lab in others:
        a = others[lab]['r'].dropna().to_numpy(float)
        b = d2_others[lab]['r'].dropna().to_numpy(float)
        print(f'  D2 {lab}: база {fmt(a)}; вариант {fmt(b)}')
    # F4
    for lab, part in (('всё', pool), ('приёмка', late)):
        fl = part[part['r'].notna()]
        print(f'  F4 фандинг по факту, {lab}: {fl["r"].mean():+.3f}R → {(fl["r"] + fl["f4_adj"]).mean():+.3f}R '
              f'(поправка {fl["f4_adj"].mean():+.3f}R/сд, лонги {fl.loc[fl.dir > 0, "f4_adj"].mean():+.3f}, '
              f'шорты {fl.loc[fl.dir < 0, "f4_adj"].mean():+.3f})')


def main_live():
    f = pd.read_pickle(os.path.join(OUT, 'live_orders.pkl'))
    pool = f[f['sample'] == 'pool'].reset_index(drop=True)
    other = f[f['sample'] == 'other'].reset_index(drop=True)
    x12 = f[f['sample'] == 'x12'].reset_index(drop=True)
    d2 = f[f['sample'] == 'pool_D2'].reset_index(drop=True)
    d2o = f[f['sample'] == 'other_D2'].reset_index(drop=True)
    d2x = f[f['sample'] == 'x12_D2'].reset_index(drop=True)
    picture(pool, 'ЯДРО БЕЗ ФИЛЬТРА ТОЛПЫ, 10 пар пула, 5 периодов')
    picture(pool[pool['crowd']], 'ЖИВАЯ SMC (фильтр толпы −1 б.п.), 10 пар пула')
    picture(other, 'ядро, 10 пар вне пула')
    # У 23 пар 12m в кэше нет фандинга — фильтр толпы на них не проверить (бот бы их
    # пропустил как «нет ставки»), поэтому для живой SMC они не участвуют.
    for label, sel, use_x in (('ядро без фильтра толпы', lambda d: d, True),
                              ('живая SMC с фильтром толпы', lambda d: d[d['crowd'] & d['fund_bp'].notna()], False)):
        P, O, X = sel(pool).reset_index(drop=True), sel(other).reset_index(drop=True), sel(x12).reset_index(drop=True)
        others = {'10 пар вне пула': O, '23 пары 12m': X} if use_x else {'10 пар вне пула': O}
        d2sel = (lambda d: d) if use_x else (lambda d: d[d['crowd']])
        others_d2 = {'10 пар вне пула': d2sel(d2o).reset_index(drop=True)}
        if use_x:
            others_d2['23 пары 12m'] = d2x.reset_index(drop=True)
        filters(P, others, label)
        execution(P, others, d2sel(d2).reset_index(drop=True), others_d2, label)


# ── Структура (S1–S4) ────────────────────────────────────────────────────────
VARIANTS = ('base', 'S1', 'S2', 'S3', 'S4')
PERIODS5 = ('bear', 'mid1', 'mid2', '12m', 'fresh')


def struct_rows(variant, period):
    if variant == 'base' and period != 'fresh':
        path = os.path.join(HERE, 'results', f'smc_lab_rows_{period}.pkl')
    else:
        path = os.path.join(OUT, f'rows_{variant}_{period}.pkl')
    f = pd.read_pickle(path).sort_values(['t', 'pair']).reset_index(drop=True)
    f['key'] = list(zip(f['pair'], f['poi_type'], f['poi_index'], f['dir']))
    return f


def struct_build(variants=VARIANTS, out_name='struct_orders.pkl'):
    import smcr_live as L
    E = L.E
    other = tuple(p for p in L.D.PAIRS if p not in L.POOL10)
    recs = []
    for period in PERIODS5:
        for v in variants:
            lay = f'st_{v}'
            E.BAR_H[lay] = 1.0
            E._rows[(period, lay)] = struct_rows(v, period)
            for sample, pairs in (('pool', L.POOL10), ('other', other)):
                spec = L.spec_for(lay, pairs)
                orders = E.orders_for(period, spec)
                res = L.simulate(period, pairs, orders)
                res_ft = L.simulate(period, pairs, orders, ft=L.FT)
                for o, a, b in zip(orders, res, res_ft):
                    feats = o.meta['feats'] or {}
                    recs.append({'variant': v, 'sample': sample, 'period': period, 'pair': o.pair,
                                 'dir': 1 if o.direction == 'BULLISH' else -1, 't': int(o.meta['row'].t),
                                 'r': None if a is None else a['pnl'] / a['risk'],
                                 'r_ft': None if b is None else b['pnl'] / b['risk'],
                                 'stop_pct': o.meta['stop_pct'], 'rr': o.meta['rr'],
                                 'crowd': not (feats.get('funding', np.nan) > -1.0),
                                 'fund_bp': feats.get('funding', np.nan)})
                print(f'  {period} {v} {sample}: заявок {len(orders)}', flush=True)
        L.simulate.cache.pop(period, None)
        E._exec.pop(period, None)
    frame = pd.DataFrame(recs)
    frame.to_pickle(os.path.join(OUT, out_name))
    return frame


def _tot(f, col='r'):
    r = f[col].dropna().to_numpy(float)
    return len(r), (r.mean() if len(r) else np.nan), r.sum()


def struct_eval(frame=None, variants=VARIANTS, out_name='struct_orders.pkl'):
    if frame is None:
        frame = pd.read_pickle(os.path.join(OUT, out_name))
    for base_label, sel in (('ядро без фильтра толпы', lambda d: d),
                            ('живая SMC с фильтром толпы', lambda d: d[d['crowd'] & d['fund_bp'].notna()])):
        print(f'\nСТРУКТУРА — {base_label}: сделок / R на сделку / итог R (каждая заявка отдельно)')
        f = sel(frame)
        for v in variants:
            pool = f[(f['variant'] == v) & (f['sample'] == 'pool')]
            oth = f[(f['variant'] == v) & (f['sample'] == 'other')]
            cells = []
            for p in PERIODS5:
                n, m, s = _tot(pool[pool['period'] == p])
                cells.append(f'{p} {n:3d} {m:+.3f} {s:+6.1f}')
            n_l, m_l, s_l = _tot(pool[late_mask(pool)])
            n_lf, m_lf, s_lf = _tot(pool[late_mask(pool)], 'r_ft')
            n_o, m_o, s_o = _tot(oth)
            print(f'  {v:4s} | ' + ' | '.join(cells) + f' || приёмка {n_l} {m_l:+.3f} {s_l:+.1f} (насквозь {s_lf:+.1f})'
                  f' || вне пула {n_o} {m_o:+.3f} {s_o:+.1f}')
        base = f[f['variant'] == 'base']
        for v in variants[1:]:
            var = f[f['variant'] == v]
            bp, vp = base[base['sample'] == 'pool'], var[var['sample'] == 'pool']
            early_ok = all(_tot(vp[vp['period'] == p])[2] > _tot(bp[bp['period'] == p])[2] for p in EARLY)
            bl, vl = bp[late_mask(bp)], vp[late_mask(vp)]
            late_sum = _tot(vl)[2] > _tot(bl)[2]
            late_mean = _tot(vl)[1] > _tot(bl)[1]
            ft_ok = _tot(vl, 'r_ft')[2] > _tot(bl, 'r_ft')[2]
            oth_ok = _tot(var[var['sample'] == 'other'])[2] > _tot(base[base['sample'] == 'other'])[2]
            lo, hi = diff_ci(vl['r'].dropna().to_numpy(float), bl['r'].dropna().to_numpy(float))
            verdict = ('не отобран (хуже в bear или mid1)' if not early_ok else
                       'ПРИНЯТ' if (late_sum and late_mean and ft_ok and oth_ok) else 'отобран, не держится')
            print(f'  {v}: отбор {"лучше" if early_ok else "нет"}; приёмка итог {"лучше" if late_sum else "хуже"}, '
                  f'на сделку {"лучше" if late_mean else "хуже"} (разница средних [{lo:+.3f}; {hi:+.3f}]), '
                  f'насквозь {"лучше" if ft_ok else "хуже"}, вне пула {"лучше" if oth_ok else "хуже"} → {verdict}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if sys.argv[1:2] == ['live']:
        main_live()
    elif sys.argv[1:2] == ['struct'] and len(sys.argv) > 2:
        # второй круг: python smcr_eval.py struct base B_htf B_any
        vs = tuple(sys.argv[2:])
        name = 'struct_orders_' + '_'.join(vs[1:]) + '.pkl'
        struct_eval(struct_build(vs, name), vs, name)
    elif sys.argv[1:2] == ['struct']:
        struct_eval(struct_build())
    elif sys.argv[1:2] == ['struct_eval']:
        struct_eval()
