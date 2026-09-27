"""
Ограничения кода поверх доктрины ИИ: какое из них делает её прибыльной.

ЗАЧЕМ (27.09.2026, docs/ИИ_аудит_2026-09-27.md). Правила промта, исполненные
кодом, теряют ~0.1R на сделку во всех пяти периодах, а модель промт держит
плохо. Ограничение в КОДЕ действует на её планы всегда, поэтому меряется на
сетапах доктрины (research/results/ai_setups_<период>.pkl, каждая заявка
исполнена отдельно по 5m, исход в R за вычетом комиссий и проскальзывания).
Признаки, державшие знак во всех периодах (results/ai_filter_study.txt):
погоня за ходом суток, фандинг в сторону сделки, дальняя первая цель.

ЗАПИСАНО ДО ЗАМЕРА. Ограничения (фандинг — б.п. за 8 ч со знаком стороны:
плюс — толпа стоит в сторону сделки и платит):
    G1  против толпы, порог 0        funding ≤ 0
    G2  против толпы, порог −1 б.п.  funding ≤ −1   (как у SMC с 27.09)
    G3  не догонять                  ret_24h ≤ 0    (вход против хода суток)
    G4  G2 + G3
    G5  G1 + G3
    G6  ближняя цель                 rr1 ≤ 3
    G7  G2 + G6
    G8  G2 + G3 + G6
    G9  толпа по трём выплатам       funding_3 ≤ −1
Приёмка:
    отбор   — лучше базы и в bear, и в mid1;
    проверка — в плюсе в mid2, 12m и fresh;
    «прибыльная» — в плюсе во всех пяти периодах, нижняя граница 95%
    интервала среднего > 0, сделок ≥ 150.
Девять правил — девять попыток: одно может пройти случайно, поэтому
прошедшее дополнительно проверяется по половинкам периодов и по сторонам.

    python research/ai_gates_study.py
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PERIODS = ('bear', 'mid1', 'mid2', '12m', 'fresh')

GATES = {
    'G0 база (доктрина)': lambda d: np.ones(len(d), bool),
    'G1 толпа 0': lambda d: d['funding'] <= 0,
    'G2 толпа −1': lambda d: d['funding'] <= -1,
    'G3 не догонять': lambda d: d['ret_24h'] <= 0,
    'G4 толпа −1 + не догонять': lambda d: (d['funding'] <= -1) & (d['ret_24h'] <= 0),
    'G5 толпа 0 + не догонять': lambda d: (d['funding'] <= 0) & (d['ret_24h'] <= 0),
    'G6 цель ≤ 3R': lambda d: d['rr1'] <= 3,
    'G7 толпа −1 + цель ≤ 3R': lambda d: (d['funding'] <= -1) & (d['rr1'] <= 3),
    'G8 толпа −1 + не догонять + цель ≤ 3R': lambda d: (d['funding'] <= -1) & (d['ret_24h'] <= 0) & (d['rr1'] <= 3),
    'G9 толпа −1 по трём выплатам': lambda d: d['funding_3'] <= -1,
}


def load():
    frames = []
    for p in PERIODS:
        d = pd.read_pickle(os.path.join(HERE, 'results', f'ai_setups_{p}.pkl'))
        d = d[(d['skeleton'] == 'doctrine') & d['filled'] & d['r'].notna()].copy()
        d['period'] = p
        frames.append(d)
    return pd.concat(frames, ignore_index=True)


def boot_ci(r, n_boot=5000, seed=7):
    r = np.asarray(r, float)
    if len(r) < 5:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    means = rng.choice(r, size=(n_boot, len(r)), replace=True).mean(axis=1)
    return np.percentile(means, 2.5), np.percentile(means, 97.5)


def main():
    d = load()
    d = d[d['funding'].notna()]
    base = {p: d[d['period'] == p]['r'].mean() for p in PERIODS}
    print(f'сетапов доктрины с фандингом: {len(d)}\n')
    head = f"{'ограничение':40s} {'сд':>5s} {'R/сд':>7s} {'95%':>17s} " + ' '.join(f'{p:>13s}' for p in PERIODS) + '  отбор проверка приб.'
    print(head)
    for name, gate in GATES.items():
        k = d[gate(d)]
        lo, hi = boot_ci(k['r'])
        per = {p: k[k['period'] == p]['r'] for p in PERIODS}
        cells = ' '.join(f"{per[p].mean():+.3f}({len(per[p]):4d})" if len(per[p]) else f"{'—':>13s}" for p in PERIODS)
        sel = all(len(per[p]) and per[p].mean() > base[p] for p in ('bear', 'mid1'))
        chk = all(len(per[p]) and per[p].mean() > 0 for p in ('mid2', '12m', 'fresh'))
        prof = all(len(per[p]) and per[p].mean() > 0 for p in PERIODS) and lo > 0 and len(k) >= 150
        print(f"{name:40s} {len(k):5d} {k['r'].mean():+7.3f} [{lo:+.3f}; {hi:+.3f}] {cells}  "
              f"{'да' if sel else 'нет':5s} {'да' if chk else 'нет':8s} {'ДА' if prof else 'нет'}")
    print('\nпо сторонам (все периоды):')
    for name in ('G0 база (доктрина)', 'G2 толпа −1', 'G4 толпа −1 + не догонять', 'G8 толпа −1 + не догонять + цель ≤ 3R'):
        k = d[GATES[name](d)]
        print(f"  {name:40s} " + '  '.join(f"{s}: {len(g):4d} сд {g['r'].mean():+.3f}R" for s, g in k.groupby('side')))


if __name__ == '__main__':
    main()
