"""
Роль P2 в портфеле тетради (30.09.2026, разбор после п. 75 — не проверка, а материал для решения).

P2 «Накопление при слабом BTC» на 2021 году ≈ 0 (−0.046R), вне выборки в целом
слабая (valid +0.16, test ≈ +0.05…0.09, 2021 −0.05), а это около половины сделок.
Портфель 6 мест, механика, на train, valid и 2021 (test не трогаем): как сейчас,
без P2, P2 половинным размером (R её сделок × 0.5).

    python research/ai_p2_role.py std     # train и valid
    python research/ai_p2_role.py 2021    # 2021 (данные research/flow_cache_2021)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODE = sys.argv[1] if len(sys.argv) > 1 else 'std'
if MODE == '2021':
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'
sys.path.insert(0, HERE)

import numpy as np                                    # noqa: E402
import pandas as pd                                   # noqa: E402

import ai_model_trader_bt as B                        # noqa: E402
import ai_pattern_lab as L                            # noqa: E402

RESULT = os.path.join(HERE, 'results', f'ai_p2_role_{MODE}.txt')


def run(data, split):
    evs = B.alerts(data, split)
    res = B.outcomes(data, evs)
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
    weeks = (b - a).days / 7
    out = []
    for name, keep, p2_size in (('как сейчас', ('P1', 'P2', 'B3'), 1.0), ('без P2', ('P1', 'B3'), 1.0),
                                ('P2 половиной', ('P1', 'P2', 'B3'), 0.5)):
        got = B.play(evs, res, lambda t, c, h, f, keep=keep: [p for p, k in c if k in keep])
        r = np.array([x[0] * (p2_size if x[1] == 'P2' else 1.0) for x in got])
        eq = np.cumsum(r)
        dd = float(np.min(eq - np.maximum.accumulate(eq))) if len(r) else 0.0
        out.append(f'  {name:13s} {len(r):4d} сд {len(r) / weeks:4.1f}/нед  сумма {r.sum():+6.1f}R  '
                   f'на сделку {r.mean():+.3f}  просадка {dd:6.1f}R  сумма/просадка {r.sum() / -dd if dd else float("nan"):4.1f}')
    return out


def main():
    lines = []
    if MODE == '2021':
        import ai_notebook_2021 as Y
        data = Y.prepare()
        splits = ['y2021']
    else:
        data = B.prepare()
        splits = ['train', 'valid']
    for split in splits:
        lines.append(f'{split}:')
        lines += run(data, split)
    print('\n'.join(lines))
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
