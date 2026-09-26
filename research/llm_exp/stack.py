"""
Добавляет ли оценка модели что-то к статистике? Проверка вне выборки по пачкам.

Если модель ошибается систематически (например, её оценка обратна исходу), это
всё равно информация: слой поверх неё может выучить, как её читать. Проверка:
логистическая регрессия исхода на [статистика] и на [статистика, модель],
обучение на половине пачек, проверка на другой половине, и наоборот.
"""
import json
import sys

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
outcomes = {o['id']: o for o in json.load(open('outcomes.json', encoding='utf-8'))}
path = sys.argv[1] if len(sys.argv) > 1 else 'results_A.jsonl'
rows = [json.loads(line) for line in open(path, encoding='utf-8')]


def fit(x, y, l2=1.0, steps=30):
    w = np.zeros(x.shape[1])
    for _ in range(steps):
        p = 1 / (1 + np.exp(-(x @ w)))
        g = x.T @ (p - y) + l2 * np.r_[0, w[1:]]
        h = (x * (p * (1 - p))[:, None]).T @ x + l2 * np.diag(np.r_[0, np.ones(len(w) - 1)]) + 1e-6 * np.eye(len(w))
        w -= np.linalg.solve(h, g)
    return w


def auc(s, y):
    pos, neg = s[y == 1], s[y == 0]
    return float(np.mean([(p > neg).mean() + 0.5 * (p == neg).mean() for p in pos])) if len(pos) and len(neg) else np.nan


batch, ml, llm, win, r = [], [], [], [], []
for row in rows:
    for i, p in row['probs'].items():
        batch.append(row['batch'])
        ml.append(outcomes[i]['ml'])
        llm.append(p)
        win.append(outcomes[i]['r'] > 0.05)
        r.append(outcomes[i]['r'])
batch, ml, llm, win, r = map(np.array, (batch, ml, llm, win, r))
z = lambda v: (v - v.mean()) / (v.std() or 1)            # noqa: E731
X1 = np.c_[np.ones(len(ml)), z(ml)]
X2 = np.c_[np.ones(len(ml)), z(ml), z(llm)]
half = (batch % 2 == 0)
res = {}
for name, X in (('статистика', X1), ('статистика + модель', X2)):
    s = np.zeros(len(win))
    for train in (half, ~half):
        w = fit(X[train], win[train].astype(float))
        s[~train] = X[~train] @ w
    res[name] = s
    top = r[s >= np.median(s)].mean()
    print(f'{name:22s} AUC вне выборки {auc(s, win.astype(int)):.3f}, верхняя половина {top:+.3f}R против всех {r.mean():+.3f}R')
w_all = fit(X2, win.astype(float))
print(f'вес модели в общей регрессии: {w_all[2]:+.3f} (статистика {w_all[1]:+.3f}); знак минус — оценка модели обратна исходу')
