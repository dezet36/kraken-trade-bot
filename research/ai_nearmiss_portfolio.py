"""
Портфель тетради v3.4 + повторившееся почти прошедшее правило (30.09.2026, п. 83).
Условие записано до расчёта: сумма R выше, R на сделку портфеля ниже не больше чем на 0.02R,
своё R правила вне выборки (valid, 2021) ≥ +0.05R. Правило торгуется на всех 42 монетах тетради.

    python research/ai_nearmiss_portfolio.py std     # train и valid
    python research/ai_nearmiss_portfolio.py 2021
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] == ['2021']:
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'      # до импорта стенда: он читает папку при импорте
sys.path.insert(0, HERE)

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import ai_new_sources as S                            # noqa: E402

L = S.install()
import ai_model_trader_bt as B                        # noqa: E402
import ai_nearmiss_replication as R                   # noqa: E402
import ai_round5 as R5                                # noqa: E402
import ai_universe_extend as U                        # noqa: E402


def main(mode):
    B.PATTERNS = dict(U.NOTEBOOK)
    if mode == '2021':
        import ai_notebook_2021 as Y
        data, splits = Y.prepare(), ['y2021']
        new = U.load('flow_cache_2021_new', data['BTCUSDT'][0], data['BTCUSDT'][1]['cascade_count_3h'], U.NEW)
    else:
        data, splits = B.prepare(), ['train', 'valid']
        new = U.load('flow_cache_new', data['BTCUSDT'][0], data['BTCUSDT'][1]['cascade_count_3h'], U.NEW)
    both = S.enrich({**data, **new})
    base = list(data)
    core = [p for p in base if p not in B.EXTRA]
    was = {k: (core if U.NOTEBOOK[k].get('universe') == 'core' else base + (list(new) if k == 'P1' else []))
           for k in U.NOTEBOOK}
    lines = [f'Портфель v3.4 (P1 на 42, 6 мест) + правило на всех монетах ({mode}): сумма R выше, R на сделку '
             f'не ниже чем −0.02R, своё R вне выборки ≥ +0.05R']
    for rule in R.candidates():
        pats = {**U.NOTEBOOK, 'N': R5.as_pattern(rule)}
        allowed = {**was, 'N': list(both)}
        lines.append(f"\n{rule['name']}:")
        for split in splits:
            a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
            weeks = (b - a).days / 7
            rows = {}
            for label, patterns in (('основа', dict(U.NOTEBOOK)), ('+ правило', pats)):
                B.PATTERNS = patterns
                got = U.portfolio(both, split, {k: allowed[k] for k in patterns})
                r = np.array([x[2] for x in got])
                eq = np.cumsum(r)
                own = np.array([x[2] for x in got if x[3] == 'N'])
                rows[label] = (r, own)
                lines.append(f"  {split:6s} {label:10s} {len(r):4d} сд {len(r) / weeks:4.1f}/нед сумма {r.sum():+6.1f}R "
                             f"на сделку {r.mean():+.3f} просадка {float(np.min(eq - np.maximum.accumulate(eq))):6.1f}R  ("
                             + ', '.join(f"{k} {sum(1 for x in got if x[3] == k)}" for k in patterns) + ')'
                             + (f"; своё {len(own)} сд {own.mean():+.3f}" if len(own) else ''))
            (r0, _), (r1, own) = rows['основа'], rows['+ правило']
            ok_sum = r1.sum() > r0.sum()
            ok_mean = r1.mean() >= r0.mean() - 0.02
            ok_own = split == 'train' or (len(own) > 0 and own.mean() >= 0.05)
            lines.append(f"    {split}: сумма выше — {'да' if ok_sum else 'НЕТ'}; R на сделку не хуже −0.02 — "
                         f"{'да' if ok_mean else 'НЕТ'}" + ('' if split == 'train' else
                                                         f"; своё R ≥ +0.05 — {'да' if ok_own else 'НЕТ'}"))
        B.PATTERNS = dict(U.NOTEBOOK)
    text = '\n'.join(lines)
    print(text, flush=True)
    with open(os.path.join(HERE, 'results', f'ai_nearmiss_portfolio_{mode}.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1])
