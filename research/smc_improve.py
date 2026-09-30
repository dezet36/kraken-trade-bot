"""
SMC без фильтра фандинга: поиск улучшений (30.09.2026).

Владелец: «нужно улучшить систему SMC. выступи в роли аналитика ... твоя
задача улучшить данную стратегию и сделать её успешной». Фандинг как
ориентир убран; ищем то, что делает саму SMC прибыльной.

ПРОТОКОЛ — ЗАПИСАН ДО ЗАМЕРА
  Данные: заявки «глазами бота» (research/smc_lab_live.py, раскладка
  'live20': окно свечей бота, его цели и R:R), 20 пар списка бота, живые
  пороги (конфлюенс 4.5, R:R 4, стоп 0.8%, предел издержек 10%, смещение
  0.1%), по заявке на зону. R — каждой заявки отдельно (без портфеля).
  Отбор — bear + mid1 (2022-01 … 2024-06); приёмка — mid2 + 12m + fresh
  (2024-07 … 2026-09); независимая проверка — 23 пары кэша 12m вне списка бота
  (раскладка 'live12x', в отборе и приёмке не участвуют).

  ФИЛЬТР. Эффект = среднее R пропущенных − среднее R всех заявок. Значимость —
  плацебо: столько же случайных заявок внутри каждой пары (период × сторона),
  20 000 раз. Кандидат — на отборе эффект > 0 и p < 0.10. Принят — на приёмке
  эффект > 0 и p < 0.05 с поправкой Холма на число кандидатов, эффект > 0 при
  наливе «насквозь», и на 23 независимых парах эффект > 0.
  ВЫХОД. Те же заявки, другой выход; парная разница R, бутстреп. Кандидат —
  на отборе средняя разница > 0. Принят — на приёмке нижняя граница 95%
  (с поправкой Бонферрони на число кандидатов) > 0, разница > 0 насквозь и на
  23 независимых парах.

ГИПОТЕЗЫ (у каждой — довод из логики SMC, а не из разбивок):
  Вход и контекст
    E1 sweep       снята ликвидность перед зоной — топливо для разворота
    E2 bos         есть слом структуры в сторону сделки — направление подтверждено
    E3 fvg         в зоне имбаланс — неэффективность, которую цена закрывает
    E4 ote         вход в OTE 0.62–0.79 ноги — «оптимальный» уровень методички
    E5 killzone    решение в сессию Лондона / Нью-Йорка — есть объём для реакции
    E6 conf5       конфлюенс от 5.0 — больше подтверждений
    E7 sweep+fvg   снятие и имбаланс вместе
    E8 sweep+bos   снятие и слом вместе
    C1 ema_side    цена по свою сторону EMA50 дня — торговля по тренду старшего
    C2 trending    трендовость пары за 30 дней ≥ 0.2 — не пила
    C3 btc_with    BTC за 7 дней шёл в сторону сделки — рынок в целом за нас
    C4 calm        волатильность ниже 75-го процентиля месяца — без паники
    C5 weekday     решение в будни — в выходные рынок тонкий
    C6 near        лимит не дальше 2% от цены — реакция свежая
    G1 rr_cap      взвешенный R:R не выше 8 — дальние цели нереалистичны
    G2 stop_adr    стоп ≥ 0.3 дневного размаха — теснее его выносит шум
  Выход
    X1 be_tp1      стоп в безубыток после первой цели
    X2 tp1_15R     первая цель на 1.5R (25%), дальше цели ядра
    X3 trail15     трейлинг 1.5R: после +1.5R стоп в безубытке, дальше следом
    X4 hold72      удержание не дольше 72 ч
    X5 one2R       одна цель на 2R — «взять своё»
    X6 be_1R       стоп в безубыток при +1R

Запуск (после smc_lab_live.py gen20 и gen12x, fetch_fresh20.py):
    python research/smc_improve.py > research/results/smc_improve.txt
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_doctrine as D                               # noqa: E402

D.PERIODS['fresh'] = 'backtest_cache_fresh20'

import smc_lab_eval as E                              # noqa: E402
from common import ci                                 # noqa: E402

for name in ('live20', 'live12x'):
    E.BAR_H.setdefault(name, 1.0)
FT = 0.0005
EARLY = ('bear', 'mid1')
LATE = ('mid2', '12m', 'fresh')
RNG = np.random.default_rng(20260930)
N_PERM = 20000


# ── Гипотезы ─────────────────────────────────────────────────────────────────
def f(row, feats, key):
    return (feats or {}).get(key, np.nan)


FILTERS = {
    'E1 sweep': lambda r, x: bool(r.f_liquidity_swept),
    'E2 bos': lambda r, x: bool(r.f_structure_break),
    'E3 fvg': lambda r, x: bool(r.f_fvg_present),
    'E4 ote': lambda r, x: bool(r.f_ote_zone),
    'E5 killzone': lambda r, x: bool(r.f_killzone),
    'E6 conf5': lambda r, x: r.confluence >= 5.0 - 1e-9,
    'E7 sweep+fvg': lambda r, x: bool(r.f_liquidity_swept) and bool(r.f_fvg_present),
    'E8 sweep+bos': lambda r, x: bool(r.f_liquidity_swept) and bool(r.f_structure_break),
    'C1 ema_side': lambda r, x: f(r, x, 'ema50d_gap') > 0,
    'C2 trending': lambda r, x: f(r, x, 'er_30d') >= 0.2,
    'C3 btc_with': lambda r, x: f(r, x, 'btc_ret_7d') > 0,
    'C4 calm': lambda r, x: f(r, x, 'atr_rank') < 75,
    'C5 weekday': lambda r, x: f(r, x, 'weekday') < 5,
    'C6 near': lambda r, x: f(r, x, 'dist_entry_pct') <= 2.0,
    'G1 rr_cap': lambda r, x: r.rr <= 8.0,
    'G2 stop_adr': lambda r, x: f(r, x, 'stop_adr') >= 0.3,
}


def _clone(o, **changes):
    kw = dict(pair=o.pair, direction=o.direction, entry=o.entry, stop=o.stop, targets=o.targets,
              fractions=o.fractions, created=o.created, expires=o.expires, key=o.key, meta=o.meta)
    kw.update(changes)
    return E.Order(**kw)


def _risk(o):
    return abs(o.entry - o.stop)


def _at_r(o, k):
    sgn = 1 if o.direction == 'BULLISH' else -1
    return o.entry + sgn * k * _risk(o)


def x_tp1_15(o):
    near = _at_r(o, 1.5)
    sgn = 1 if o.direction == 'BULLISH' else -1
    farther = [t for t in o.targets if (t - near) * sgn > 0]
    tg = ([near] + farther)[:3]
    fr = [0.25, 0.25, 0.5][:len(tg)]
    fr[-1] += 1.0 - sum(fr)
    return _clone(o, targets=tg, fractions=fr)


EXITS = {
    'X1 be_tp1': (lambda o: o, {'breakeven_after_tp1': True}),
    'X2 tp1_15R': (x_tp1_15, {}),
    'X3 trail15': (lambda o: _clone(o, trail_distance=1.5 * _risk(o)), {}),
    'X4 hold72': (lambda o: o, {'max_hold_hours': 72.0}),
    'X5 one2R': (lambda o: _clone(o, targets=[_at_r(o, 2.0)], fractions=[1.0]), {}),
    'X6 be_1R': (lambda o: _clone(o, be_trigger=_at_r(o, 1.0)), {}),
}


# ── Заявки и их R ────────────────────────────────────────────────────────────
def build(periods, layout, pairs):
    """Заявки живых правил (по одной на зону) с R базового выхода и каждого варианта."""
    spec = dict(E.LIVE, pairs=pairs, layouts=(layout,), filt=lambda r, x: True)
    recs = []
    for period in periods:
        orders = E.orders_for(period, spec)
        data = E.exec_data(period, pairs)
        prepared = {p: E._prepare(df) for p, df in data.items()}

        def sim(o, ft, **kw):
            arr = prepared.get(o.pair)
            if arr is None:
                return None
            start = int(np.searchsorted(arr['ts'], np.datetime64(o.created), side='right'))
            E.smc_engine.FILL_THROUGH_PCT = ft
            try:
                args = dict(breakeven_after_tp1=False, max_hold_hours=336.0)
                args.update(kw)
                res = E.simulate_order(o, arr, start, 100.0, **args)
            finally:
                E.smc_engine.FILL_THROUGH_PCT = 0.0
            return None if res is None else res['pnl'] / res['risk']

        for o in orders:
            row, feats = o.meta['row'], o.meta['feats']
            rec = {'period': period, 'pair': o.pair, 'dir': int(row.dir), 'row': row, 'feats': feats,
                   'r': sim(o, 0.0), 'r_ft': sim(o, FT)}
            if rec['r'] is None:
                recs.append(rec)
                continue
            for name, (make, kw) in EXITS.items():
                alt = make(o)
                rec[name] = sim(alt, 0.0, **kw)
                rec[name + ' ft'] = sim(alt, FT, **kw)
            recs.append(rec)
        print(f'  {layout} {period}: заявок {len(orders)}', flush=True)
    frame = pd.DataFrame(recs)
    for name, rule in FILTERS.items():
        frame[name] = [bool(rule(r, x)) if x is not None else False for r, x in zip(frame['row'], frame['feats'])]
    return frame


# ── Статистика ───────────────────────────────────────────────────────────────
def placebo(frame, mask, col='r'):
    """Эффект фильтра и p: случайный отбор того же размера внутри (период × сторона)."""
    part = frame[frame[col].notna()].reset_index(drop=True)
    m = mask[frame[col].notna()].to_numpy()
    r = part[col].to_numpy(dtype=float)
    if m.sum() < 5:
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


def paired(frame, name, col_base='r'):
    ok = frame[col_base].notna() & frame[name].notna()
    d = (frame.loc[ok, name] - frame.loc[ok, col_base]).to_numpy(dtype=float)
    if len(d) < 10:
        return np.nan, (np.nan, np.nan), len(d)
    boot = RNG.choice(d, size=(5000, len(d)), replace=True).mean(axis=1)
    return d.mean(), boot, len(d)


def describe(frame, mask=None, col='r'):
    part = frame if mask is None else frame[mask]
    r = part[col].dropna().to_numpy(dtype=float)
    if len(r) < 5:
        return f'{len(r):4d} сд'
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    return f'{len(r):4d} сд, в плюс {np.mean(r > 0):4.0%}, {r.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}], итог {r.sum():+7.1f}R'


def main():
    pairs20 = tuple(D.PAIRS)
    print('Заявки «глазами бота», 20 пар, живые пороги, без фильтра фандинга')
    full = build(E.PERIODS, 'live20', pairs20)
    x12 = build(('12m',), 'live12x', tuple(sorted(set(E.rows('12m', 'live12x')['pair']))))
    full.drop(columns=['row', 'feats']).to_pickle(os.path.join(E.OUT, 'smc_improve_setups.pkl'))
    early, late = full[full['period'].isin(EARLY)], full[full['period'].isin(LATE)]
    print(f'\nБАЗА: отбор {describe(early)}\n      приёмка {describe(late)}\n      23 пары 12m {describe(x12)}')

    print('\nФИЛЬТРЫ (эффект — к среднему R всех заявок; p — плацебо внутри период × сторона)')
    cands = []
    for name in FILTERS:
        eff, p, n = placebo(early, early[name])
        keep = np.isfinite(eff) and eff > 0 and p < 0.10
        print(f'  {name:14s} отбор: {n:4d} сд, эффект {eff:+.3f}, p {p:.3f}  {"→ кандидат" if keep else ""}', flush=True)
        if keep:
            cands.append(name)
    accepted = []
    if cands:
        print(f'\n  ПРИЁМКА кандидатов ({len(cands)}), поправка Холма:')
        tested = []
        for name in cands:
            eff, p, n = placebo(late, late[name])
            eff_ft, p_ft, _ = placebo(late, late[name], col='r_ft')
            eff_x, p_x, n_x = placebo(x12, x12[name])
            tested.append((p, name, eff, n, eff_ft, eff_x, p_x, n_x))
        tested.sort()
        k = len(tested)
        for i, (p, name, eff, n, eff_ft, eff_x, p_x, n_x) in enumerate(tested):
            limit = 0.05 / (k - i)
            ok = eff > 0 and p < limit and eff_ft > 0 and eff_x > 0
            print(f'  {name:14s} приёмка: {n:4d} сд, эффект {eff:+.3f}, p {p:.4f} (порог {limit:.4f}), '
                  f'насквозь {eff_ft:+.3f}; 23 пары: {n_x} сд, эффект {eff_x:+.3f}, p {p_x:.3f}  '
                  f'{"ПРИНЯТ" if ok else "нет"}', flush=True)
            if ok:
                accepted.append(name)

    print('\nВЫХОДЫ (парная разница R с базовым выходом, те же заявки)')
    xcands = []
    for name in EXITS:
        d, boot, n = paired(early, name)
        keep = np.isfinite(d) and d > 0
        print(f'  {name:12s} отбор: {n:4d} сд, разница {d:+.3f} [{np.percentile(boot, 2.5):+.3f}; '
              f'{np.percentile(boot, 97.5):+.3f}]  {"→ кандидат" if keep else ""}', flush=True)
        if keep:
            xcands.append(name)
    xaccepted = []
    for name in xcands:
        q = 0.05 / len(xcands)
        d, boot, n = paired(late, name)
        d_ft, _, _ = paired(late, name + ' ft', col_base='r_ft')
        d_x, boot_x, n_x = paired(x12, name)
        lo = np.percentile(boot, 100 * q / 2)
        ok = lo > 0 and d_ft > 0 and d_x > 0
        print(f'  {name:12s} приёмка: {n:4d} сд, разница {d:+.3f}, нижняя граница {lo:+.3f} (поправка на '
              f'{len(xcands)}), насквозь {d_ft:+.3f}; 23 пары: {d_x:+.3f}  {"ПРИНЯТ" if ok else "нет"}', flush=True)
        if ok:
            xaccepted.append(name)

    print('\nИТОГ: фильтры —', ', '.join(accepted) or 'ни один', '; выходы —', ', '.join(xaccepted) or 'ни один')
    for name in accepted:
        print(f'  {name}: отбор {describe(early, early[name])}')
        print(f'  {"":{len(name)}s}  приёмка {describe(late, late[name])}')
        print(f'  {"":{len(name)}s}  23 пары {describe(x12, x12[name])}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
