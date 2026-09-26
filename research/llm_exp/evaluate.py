"""Сверка ответов модели с исходами: отбор модели против статистики и случая."""
import json
import sys

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
outcomes = {o['id']: o for o in json.load(open('outcomes.json', encoding='utf-8'))}
path = sys.argv[1] if len(sys.argv) > 1 else 'results_A.jsonl'
rows = [json.loads(line) for line in open(path, encoding='utf-8')]
rng = np.random.default_rng(1)


def auc(scores, labels):
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return float('nan')
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


llm_top, ml_top, all_r, diffs, rand = [], [], [], [], []
scores, mls, labels, rs, probs = [], [], [], [], []
for row in rows:
    ids = list(row['probs'])
    if len(ids) < 10:
        continue
    r = np.array([outcomes[i]['r'] for i in ids])
    p = np.array([row['probs'][i] for i in ids], dtype=float)
    m = np.array([outcomes[i]['ml'] for i in ids])
    # Ничьи у модели — случайным порядком, иначе «верхние 5» зависят от места в пачке.
    top_llm = np.argsort(-(p + rng.random(len(p)) * 1e-6))[:5]
    top_ml = np.argsort(-m)[:5]
    llm_top.append(r[top_llm].mean())
    ml_top.append(r[top_ml].mean())
    all_r.append(r.mean())
    diffs.append(r[top_llm].mean() - r.mean())
    rand.append(np.mean([r[rng.permutation(10)[:5]].mean() for _ in range(200)]))
    scores += list(p)
    mls += list(m)
    labels += list(r > 0.05)
    rs += list(r)
n = len(llm_top)
print(f'пачек {n}, сетапов {len(rs)}; средний R {np.mean(rs):+.3f}, доля в плюс {np.mean(labels) * 100:.0f}%')
print(f'верхние 5 из 10 — модель {np.mean(llm_top):+.3f}R, статистика {np.mean(ml_top):+.3f}R, все {np.mean(all_r):+.3f}R')
d = np.array(llm_top) - np.array(all_r)
se = d.std(ddof=1) / np.sqrt(n)
print(f'модель минус все: {d.mean():+.3f}R ± {1.96 * se:.3f} (95%)')
d2 = np.array(llm_top) - np.array(ml_top)
se2 = d2.std(ddof=1) / np.sqrt(n)
print(f'модель минус статистика: {d2.mean():+.3f}R ± {1.96 * se2:.3f} (95%)')
print(f'AUC (исход > 0): модель {auc(scores, labels):.3f}, статистика {auc(mls, labels):.3f}')
sc = np.array(scores)
print(f'вероятности модели: среднее {sc.mean():.1f}%, разброс {sc.std():.1f}, уникальных значений {len(set(scores))}')
for lo, hi in ((0, 30), (30, 40), (40, 50), (50, 101)):
    m = (sc >= lo) & (sc < hi)
    if m.sum():
        print(f'   модель {lo}–{hi}%: {m.sum():3d} сетапов, на деле в плюс {np.mean(np.array(labels)[m]) * 100:.0f}%, '
              f'средний R {np.mean(np.array(rs)[m]):+.3f}')
