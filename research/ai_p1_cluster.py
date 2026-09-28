"""
Сколько сигналов P1 брать из одного каскада (28.09.2026, разведка к docs п. 72).

Модель берёт около трети сигналов P1 («Отскок после ликвидаций»): тетрадь велит
«позиции одного движения рынка — одна ставка, бери сильнейшие». Выбор внутри
каскада у неё не лучше «брать все» (S_P: +0.37R против +0.41R). Здесь — без
модели, весь портфель (6 мест, P2 и B3 как есть) на train и valid: брать все
P1 подряд, только k самых глубоких, только k с наибольшим счётом «лучше/хуже»
(п. 71) или только со счётом ≥ m. Это разведка: решение о тетради — отдельной
проверкой с моделью; test не трогаем.

    python research/ai_p1_cluster.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_model_trader_bt as B                        # noqa: E402
import ai_notebook_flags as F                         # noqa: E402
import ai_pattern_lab as L                            # noqa: E402

RESULT = os.path.join(HERE, 'results', 'ai_p1_cluster.txt')


def policies(data):
    def score(t, p, k):
        return F.score(data[p][1].loc[t], k)

    def keep_p1(t, cand, pick):
        p1 = [(p, k) for p, k in cand if k == 'P1']
        rest = [(p, k) for p, k in cand if k != 'P1']
        return [p for p, _ in pick(t, p1)] + [p for p, _ in rest]

    out = {'все подряд (механика)': lambda t, c, h, f: [p for p, _ in c]}
    for k in (1, 2, 3):
        out[f'P1: {k} самых глубоких'] = (lambda kk: lambda t, c, h, f: keep_p1(t, c, lambda t_, p1: p1[:kk]))(k)
    for k in (1, 2, 3):
        out[f'P1: {k} с большим счётом'] = (lambda kk: lambda t, c, h, f: keep_p1(
            t, c, lambda t_, p1: sorted(p1, key=lambda x: -score(t_, x[0], 'P1'))[:kk]))(k)
    for m in (2, 3):
        out[f'P1: только счёт ≥ {m}'] = (lambda mm: lambda t, c, h, f: keep_p1(
            t, c, lambda t_, p1: [x for x in p1 if score(t_, x[0], 'P1') >= mm]))(m)
    return out


def stats(got, weeks):
    r = np.array([x[0] for x in got])
    if not len(r):
        return '0 сд'
    eq = np.cumsum(r)
    dd = float(np.min(eq - np.maximum.accumulate(eq)))
    p1 = np.array([x[0] for x in got if x[1] == 'P1'])
    t = r.mean() / r.std(ddof=1) * np.sqrt(len(r)) if len(r) > 1 else float('nan')
    return (f'{len(r):4d} сд {len(r) / weeks:4.1f}/нед R {r.mean():+.3f} t {t:+.1f} сумма {r.sum():+6.1f}R '
            f'просадка {dd:6.1f}R | P1 {len(p1):3d} сд {p1.mean() if len(p1) else float("nan"):+.3f}')


def main():
    lines = []

    def say(text=''):
        print(text, flush=True)
        lines.append(text)

    data = B.prepare()
    say('Сколько P1 брать из каскада: портфель 6 мест, P2 и B3 — как есть (разведка к п. 72)')
    for split in ('train', 'valid'):
        evs = B.alerts(data, split)
        res = B.outcomes(data, evs)
        a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
        weeks = (b - a).days / 7
        say(f'\n{split}:')
        for name, choose in policies(data).items():
            say(f'  {name:26s} {stats(B.play(evs, res, choose), weeks)}')
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
