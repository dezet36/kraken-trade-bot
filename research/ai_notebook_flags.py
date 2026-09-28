"""
Несут ли «лучше/хуже» тетради пользу вне train (28.09.2026, docs/ИИ_замечания_на_проверку.md, п. 71).

Флаги — ровно по тексту тетради (llm_notebook.NOTEBOOK), пороги записаны до
расчёта. Счёт сигнала = число «лучше» − число «хуже». Сигналы — срезы стенда
(ai_question_v31.slices): начало серии, пары не в позиции у механики. Условие
приёмки — на valid; train — справочно (там флаги и выведены); test не трогаем.

    python research/ai_notebook_flags.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_model_trader_bt as B                        # noqa: E402
import ai_question_v31 as Q                           # noqa: E402

RESULT = os.path.join(HERE, 'results', 'ai_notebook_flags.txt')
OPS = {'<': np.less, '<=': np.less_equal, '>': np.greater, '>=': np.greater_equal}

FLAGS = {
    'P1': {'better': [('cascade_count_3h', '>=', 6), ('vol_z', '>', 1.5), ('btc_ret_24h', '<', -2.0),
                      ('ret_7d', '<', -25.0), ('funding_bp', '>', 1.0)],
           'worse': [('abs_btc_ret_24h', '<', 1.0), ('vol_z', '<', 1.2)]},
    'P2': {'better': [('btc_ret_7d', '>', 3.4), ('btc_ret_24h', '<', -2.1), ('ret_7d', '>', 16.0), ('vol_z', '>', 2.8)],
           'worse': [('btc_ret_7d', '<', 0.0)]},
    'B3': {'better': [('buy_ratio_pct_30d', '>', 0.5), ('funding_bp', '<=', 0.0), ('btc_ret_30d', '>', -9.0)],
           'worse': [('btc_ret_30d', '<=', -9.0), ('vol_z', '>', 2.1)]},
}


def value(f, name):
    return abs(f['btc_ret_24h']) if name == 'abs_btc_ret_24h' else f[name]


def score(f, key):
    s = 0
    for side, sign in (('better', 1), ('worse', -1)):
        for name, op, v in FLAGS[key][side]:
            x = value(f, name)
            if pd.notna(x) and OPS[op](float(x), v):
                s += sign
    return s


def welch(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2 or len(b) < 2:
        return float('nan'), float('nan')
    diff = a.mean() - b.mean()
    return diff, diff / np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))


def rows_of(data, split):
    slices, res = Q.slices(data, split)
    out = []
    for s in slices:
        for p, k in s['items']:
            f = data[p][1].loc[s['t']]
            out.append({'split': split, 'key': k, 'pair': p, 't': s['t'], 'score': score(f, k),
                        'r': res[(s['t'], p)][0]})
    return out


def main():
    lines = []

    def say(text=''):
        print(text, flush=True)
        lines.append(text)

    data = B.prepare()
    d = pd.DataFrame(rows_of(data, 'train') + rows_of(data, 'valid'))
    say('Флаги «лучше/хуже» тетради: R сигналов по счёту (п. 71)')
    for split in ('train', 'valid'):
        s = d[d.split == split]
        say(f'\n{split}: сигналов {len(s)}')
        for key in ('P1', 'P2', 'B3'):
            k = s[s.key == key]
            by = ', '.join(f'{sc:+d}: {len(g)} сд {g.r.mean():+.3f}' for sc, g in k.groupby('score'))
            diff, t = welch(k[k.score > 0].r, k[k.score <= 0].r)
            say(f'  {key}: {len(k)} сд R {k.r.mean():+.3f} | счёт > 0 минус ≤ 0: {diff:+.3f} (t {t:+.1f}) | {by}')
        diff, t = welch(s[s.score > 0].r, s[s.score <= 0].r)
        say(f'  все: счёт > 0 {int((s.score > 0).sum())} сд {s[s.score > 0].r.mean():+.3f}, '
            f'≤ 0 {int((s.score <= 0).sum())} сд {s[s.score <= 0].r.mean():+.3f}; разница {diff:+.3f} (t {t:+.1f})')

    v = d[d.split == 'valid']
    diff, t = welch(v[v.score > 0].r, v[v.score <= 0].r)
    d1, _ = welch(v[(v.key == 'P1') & (v.score > 0)].r, v[(v.key == 'P1') & (v.score <= 0)].r)
    d2, _ = welch(v[(v.key == 'P2') & (v.score > 0)].r, v[(v.key == 'P2') & (v.score <= 0)].r)
    c1 = diff >= 0.10 and t >= 2.0
    c2 = d1 > 0 and d2 > 0
    say(f"\n  {'да ' if c1 else 'НЕТ'} 1. valid, все: разница {diff:+.3f}R, t {t:+.1f} (нужно ≥ +0.10 и t ≥ 2)")
    say(f"  {'да ' if c2 else 'НЕТ'} 2. valid: разница P1 {d1:+.3f}, P2 {d2:+.3f} (нужно обе > 0)")
    say(f"\nИтог: {'флаги несут пользу вне train' if c1 and c2 else 'пользы вне train не видно'}")
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
