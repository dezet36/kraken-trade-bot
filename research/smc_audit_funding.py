"""
Аудит фильтра «против толпы» у SMC (30.09.2026).

Владелец: «на сколько этот фильтр работает? фильтр фандинга. давай проверим».
Фильтр в боте с 27.09.2026 (strategy_smc.crowd_reason, smc/params):
ставка фандинга в сторону сделки ≤ −1 б.п. — лонг только при ставке ≤ −1 б.п.
(толпа в шортах), шорт только при ставке ≥ +1 б.п. (середина ставки Bybit и
выше). Нет ставки — не отказ.

Что проверяется (стенд research/smc_lab: живые правила, 10 пар, 5 периодов):
  1. Где лежит плюс: R каждой заявки отдельно (без портфеля) по СЫРОЙ ставке,
     лонги и шорты отдельно.
  2. Плацебо: отбор того же размера, но случайный — внутри каждой пары
     (период × сторона). Фильтр лучше 95% случайных — ставка несёт сведения
     сверх того, лонг это или шорт и какой был год.
  3. Не «просто шорты» ли это: портфель с живыми правилами — база, только
     шорты, только лонги, фильтр целиком, фильтр только на одной стороне.
  4. Время: полугодия — пропущенные фильтром против отсечённых.
  5. Пары: то же по каждой паре.
  6. Порог: от −3 до +2 б.п. — заявки отдельно и портфель.
Налив «касанием» и «насквозь на 0.05%» — везде, где считается портфель.

Запуск (после research/smc_lab.py gen):
    python research/smc_audit_funding.py > research/results/smc_audit_funding.txt
    python research/smc_audit_funding.py live > research/results/smc_audit_funding_live.txt
Второй — на срабатываниях «глазами бота» (research/smc_lab_live.py: окно свечей
живого бота; цели там такие же, как у бота, у стенда по всей истории — нет).
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_lab_eval as E                              # noqa: E402
from common import ci                                 # noqa: E402

LIMIT = -1.0                                           # порог бота, б.п. в сторону сделки
FT = 0.0005                                            # налив насквозь на 0.05%
RNG = np.random.default_rng(20260930)
N_PERM = 20000


def signed(f):
    return (f or {}).get('funding', np.nan)


def passes(x, limit=LIMIT):
    return not (x > limit)                             # нет ставки — не отказ, как в боте


def fmt_ci(r):
    r = np.asarray(r, dtype=float)
    if len(r) < 10:
        return f'{len(r):4d} сд {r.sum() if len(r) else 0:+7.1f}R {r.mean() if len(r) else 0:+.3f}'
    lo, hi = ci(r)
    return f'{len(r):4d} сд {r.sum():+7.1f}R {r.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]'


# ── Заявки отдельно ──────────────────────────────────────────────────────────
def setups():
    """Каждая заявка живых правил отдельно: сторона, ставка, R касанием и насквозь."""
    spec = dict(E.LIVE, filt=lambda r, f: True)        # пустышка — чтобы посчитать признаки
    out = []
    for period in E.PERIODS:
        orders = E.orders_for(period, spec)
        touch = E.per_setup(period, spec, orders)
        through = E.per_setup(period, dict(spec, fill_through=FT), orders)
        for (o, r1), (_, r2) in zip(touch, through):
            row = o.meta['row']
            s = signed(o.meta['feats'])
            out.append({'period': period, 'pair': o.pair, 'dir': int(row.dir),
                        't': pd.Timestamp(o.created, tz='UTC'), 'signed': s,
                        'raw': s * row.dir if np.isfinite(s) else np.nan,
                        'r': r1, 'r_ft': r2})
    f = pd.DataFrame(out)
    f['pass'] = [passes(x) for x in f['signed']]
    f['pass0'] = [passes(x, 0.0) for x in f['signed']]
    return f


def raw_bucket(x):
    if not np.isfinite(x):
        return 'нет ставки'
    if x <= -1.0 + 1e-9:
        return '≤ −1'
    if x < 0:
        return '(−1; 0)'
    if x < 1.0 - 0.005:
        return '[0; 1)'
    if x <= 1.0 + 0.005:
        return 'ровно +1'
    return '> +1'


BUCKETS = ['≤ −1', '(−1; 0)', '[0; 1)', 'ровно +1', '> +1', 'нет ставки']


def table_by_rate(f):
    print('1. ГДЕ ПЛЮС: R каждой заявки отдельно (налив касанием) по сырой ставке, б.п. за 8 ч')
    print('   фильтр пропускает: лонг при ставке ≤ −1, шорт при ставке ≥ +1 (и без ставки)')
    filled = f[f['r'].notna()].copy()
    filled['bucket'] = [raw_bucket(x) for x in filled['raw']]
    for side, name in ((1, 'ЛОНГИ'), (-1, 'ШОРТЫ')):
        print(f'  {name}')
        part = filled[filled['dir'] == side]
        for b in BUCKETS:
            rs = part.loc[part['bucket'] == b, 'r']
            if len(rs):
                share = part.loc[part['bucket'] == b, 'pass'].mean()
                ok = 'пропуск' if share == 1 else 'отказ' if share == 0 else f'пропуск {share:.0%}'
                print(f'    ставка {b:10s} {ok:12s} {fmt_ci(rs)}')
        print(f'    {"все":17s} {fmt_ci(part["r"])}')
    print(f'  ВСЕ ЗАЯВКИ: пропущенные {fmt_ci(filled.loc[filled["pass"], "r"])}')
    print(f'              отсечённые  {fmt_ci(filled.loc[~filled["pass"], "r"])}')
    print(f'  насквозь:   пропущенные {fmt_ci(filled.loc[filled["pass"] & filled["r_ft"].notna(), "r_ft"])}')
    print(f'              отсечённые  {fmt_ci(filled.loc[~filled["pass"] & filled["r_ft"].notna(), "r_ft"])}')
    share = f.groupby('dir')['pass'].mean()
    print(f'  доля заявок, которые фильтр пропускает: лонги {share.get(1, 0):.0%}, шорты {share.get(-1, 0):.0%}')
    print()


def placebo(f, column='pass', label='порог −1 б.п.'):
    """Случайный отбор того же размера внутри страт; p — доля не хуже фильтра."""
    filled = f[f['r'].notna()].reset_index(drop=True)
    r = filled['r'].to_numpy()
    actual = r[filled[column].to_numpy()].mean()
    base = r.mean()
    for strata_cols, name in ((['period', 'dir'], 'период × сторона'), (['period'], 'только период')):
        groups = [np.asarray(idx) for idx in filled.groupby(strata_cols).indices.values()]
        ks = [int(filled.loc[g, column].sum()) for g in groups]
        sims = np.empty(N_PERM)
        for i in range(N_PERM):
            total, count = 0.0, 0
            for g, k in zip(groups, ks):
                if k:
                    pick = RNG.choice(g, size=k, replace=False)
                    total += r[pick].sum()
                    count += k
            sims[i] = total / count
        p = (np.sum(sims >= actual) + 1) / (N_PERM + 1)
        print(f'  {label}, случайный отбор внутри «{name}»: фильтр {actual:+.3f} R/сд, случайные '
              f'{np.mean(sims):+.3f} [{np.percentile(sims, 2.5):+.3f}; {np.percentile(sims, 97.5):+.3f}], '
              f'p = {p:.4f}  (все заявки {base:+.3f})')


def by_time(f):
    print('4. ВРЕМЯ: полугодия — пропущенные против отсечённых (заявки отдельно, касанием)')
    filled = f[f['r'].notna()].copy()
    filled['half'] = [f'{t.year}-{"I" if t.month <= 6 else "II"}' for t in filled['t']]
    wins = 0
    rows = 0
    for half, part in filled.groupby('half'):
        a, b = part.loc[part['pass'], 'r'], part.loc[~part['pass'], 'r']
        diff = (a.mean() if len(a) else np.nan) - (b.mean() if len(b) else np.nan)
        if np.isfinite(diff):
            rows += 1
            wins += diff > 0
        print(f'  {half:8s} пропущенные {len(a):3d} сд {a.mean() if len(a) else np.nan:+.3f}   '
              f'отсечённые {len(b):3d} сд {b.mean() if len(b) else np.nan:+.3f}   разница {diff:+.3f}')
    print(f'  пропущенные лучше в {wins} полугодиях из {rows}')
    print()


def by_pair(f):
    print('5. ПАРЫ: пропущенные против отсечённых (заявки отдельно, касанием)')
    filled = f[f['r'].notna()]
    wins = 0
    for pair, part in filled.groupby('pair'):
        a, b = part.loc[part['pass'], 'r'], part.loc[~part['pass'], 'r']
        diff = a.mean() - b.mean()
        wins += diff > 0
        print(f'  {pair:10s} пропущенные {len(a):3d} сд {a.mean():+.3f} (Σ {a.sum():+6.1f}R)   '
              f'отсечённые {len(b):3d} сд {b.mean():+.3f} (Σ {b.sum():+6.1f}R)')
    print(f'  пропущенные лучше на {wins} парах из {filled["pair"].nunique()}')
    print()


def thresholds_per_setup(f):
    print('6а. ПОРОГ: заявки отдельно (касанием), пропуск при ставке в сторону сделки ≤ порога')
    filled = f[f['r'].notna()]
    for limit in (-3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
        m = np.array([passes(x, limit) for x in filled['signed']])
        print(f'  порог {limit:+.1f}: {fmt_ci(filled.loc[m, "r"])}')
    print(f'  без фильтра: {fmt_ci(filled["r"])}')
    print()


# ── Портфель с живыми правилами ──────────────────────────────────────────────
def against(limit):
    return lambda r, f: passes(signed(f), limit)


VARIANTS = [
    ('база: без фильтра', None),
    ('только шорты', lambda r, f: r.dir == -1),
    ('только лонги', lambda r, f: r.dir == 1),
    ('ФИЛЬТР −1 б.п. (как в боте)', against(-1.0)),
    ('шорты с фильтром, лонгов нет', lambda r, f: r.dir == -1 and passes(signed(f))),
    ('лонги с фильтром, шортов нет', lambda r, f: r.dir == 1 and passes(signed(f))),
    ('фильтр только на шортах (лонги все)', lambda r, f: r.dir == 1 or passes(signed(f))),
    ('фильтр только на лонгах (шорты все)', lambda r, f: r.dir == -1 or passes(signed(f))),
]


def portfolio_table():
    print('3. НЕ «ПРОСТО ШОРТЫ» ЛИ: портфель с живыми правилами (кэп 3, 5 позиций, срок 48 ч)')
    for label, filt in VARIANTS:
        E.run({'filt': filt}, label)
        E.run({'filt': filt, 'fill_through': FT}, '   … налив насквозь')
    print()


def thresholds_portfolio():
    print('6б. ПОРОГ: портфель с живыми правилами')
    for limit in (-3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0):
        E.run({'filt': against(limit)}, f'порог {limit:+.1f}')
    print()


def use_layout(layout):
    """'1h' — стенд по всей истории; 'live' — срабатывания на окне свечей бота."""
    if layout != '1h':
        E.BAR_H.setdefault(layout, 1.0)
        E.LIVE = dict(E.LIVE, layouts=(layout,))


def slots_table():
    print('7. МЕСТА: у стенда 5 позиций, у бота предела нет (max_slots 0) — остаётся только кэп 3 в сторону')
    for label, change in (('фильтр, 5 позиций', {}), ('фильтр, без предела позиций', {'max_positions': 99})):
        E.run(dict(change, filt=against(-1.0)), label)
        E.run(dict(change, filt=against(-1.0), fill_through=FT), '   … налив насквозь')
    print()


def main():
    layout = sys.argv[1] if len(sys.argv) > 1 else '1h'
    use_layout(layout)
    print(f'Срабатывания: {"окно свечей бота (smc_lab_live)" if layout == "live" else "стенд по всей истории (smc_lab)"}')
    f = setups()
    f.to_pickle(os.path.join(E.OUT, f'smc_audit_setups{"" if layout == "1h" else "_" + layout}.pkl'))
    print(f'Заявок живых правил: {len(f)}, налилось {int(f["r"].notna().sum())}; '
          f'без ставки {int(f["signed"].isna().sum())}\n')
    table_by_rate(f)
    print('2. ПЛАЦЕБО: случайный отбор того же размера (20 000 раз)')
    placebo(f, 'pass', 'порог −1 б.п. (как в боте)')
    placebo(f, 'pass0', 'порог 0 (записан до замера)')
    print()
    portfolio_table()
    by_time(f)
    by_pair(f)
    thresholds_per_setup(f)
    thresholds_portfolio()
    slots_table()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
