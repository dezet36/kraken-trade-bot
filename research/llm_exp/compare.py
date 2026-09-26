"""A против B на одних и тех же пачках: помогает ли модели знание того, что работает."""
import json
import sys

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
outcomes = {o['id']: o for o in json.load(open('outcomes.json', encoding='utf-8'))}
rng = np.random.default_rng(2)


def load(path):
    out = {}
    for line in open(path, encoding='utf-8'):
        row = json.loads(line)
        if len(row['probs']) == 10:
            out[row['batch']] = row['probs']
    return out


def auc(scores, labels):
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg)) if pos and neg else float('nan')


a, b = load('results_A.jsonl'), load('results_B.jsonl')
common = sorted(set(a) & set(b))
top = {'A': [], 'B': [], 'ML': [], 'все': []}
scores = {'A': [], 'B': [], 'ML': []}
labels, rs = [], []
for k in common:
    ids = list(a[k])
    r = np.array([outcomes[i]['r'] for i in ids])
    for name, src in (('A', a[k]), ('B', b[k])):
        p = np.array([src[i] for i in ids], dtype=float)
        top[name].append(r[np.argsort(-(p + rng.random(10) * 1e-6))[:5]].mean())
        scores[name] += list(p)
    m = np.array([outcomes[i]['ml'] for i in ids])
    top['ML'].append(r[np.argsort(-m)[:5]].mean())
    scores['ML'] += list(m)
    top['все'].append(r.mean())
    labels += list(r > 0.05)
    rs += list(r)
n = len(common)
print(f'общих пачек {n}, сетапов {len(rs)}; средний R {np.mean(rs):+.3f}, доля в плюс {np.mean(labels) * 100:.0f}%')
for name in ('A', 'B', 'ML'):
    d = np.array(top[name]) - np.array(top['все'])
    print(f'  {name:3s}: верхние 5 из 10 {np.mean(top[name]):+.3f}R (к «брать всё» {d.mean():+.3f} ± {1.96 * d.std(ddof=1) / np.sqrt(n):.3f}), '
          f'AUC {auc(scores[name], labels):.3f}')
d = np.array(top['B']) - np.array(top['A'])
print(f'  B минус A: {d.mean():+.3f}R ± {1.96 * d.std(ddof=1) / np.sqrt(n):.3f} (95%)')
d = np.array(top['B']) - np.array(top['ML'])
print(f'  B минус статистика: {d.mean():+.3f}R ± {1.96 * d.std(ddof=1) / np.sqrt(n):.3f} (95%)')
sb = np.array(scores['B'])
print(f'  оценки B: среднее {sb.mean():.1f}%, разброс {sb.std():.1f}; корреляция с оценками A '
      f'{np.corrcoef(scores["A"], scores["B"])[0, 1]:+.2f}, со статистикой {np.corrcoef(scores["ML"], scores["B"])[0, 1]:+.2f}')
