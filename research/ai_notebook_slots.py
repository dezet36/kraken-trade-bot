"""
Сколько мест тетради (30.09.2026): 6, 8 или 10 одновременных позиций — справка для решения владельца.

Не проверка правила: закономерности те же (v3.4, P1 на 42 монетах — п. 80), меняется только
число мест. Больше мест — больше сделок каскада той же доходности на сделку, но и больше
одновременного риска в одном событии («худший час входа» — сумма R сделок, открытых в один час).

    python research/ai_notebook_slots.py std     # train и valid
    python research/ai_notebook_slots.py 2021
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
mode = sys.argv[1]
if mode == '2021':
    os.environ['FLOW_CACHE'] = 'flow_cache_2021'
sys.path.insert(0, HERE)
sys.argv = [sys.argv[0], mode]
import numpy as np, pandas as pd
import ai_universe_extend as U
import ai_model_trader_bt as B
import ai_pattern_lab as L
sys.stdout.reconfigure(encoding='utf-8')
lines = []
B.PATTERNS = dict(U.NOTEBOOK)
if mode == '2021':
    import ai_notebook_2021 as Y
    data = Y.prepare(); splits = ['y2021']
    new = U.load('flow_cache_2021_new', data['BTCUSDT'][0], data['BTCUSDT'][1]['cascade_count_3h'], U.NEW)
else:
    data = B.prepare(); splits = ['train', 'valid']
    new = U.load('flow_cache_new', data['BTCUSDT'][0], data['BTCUSDT'][1]['cascade_count_3h'], U.NEW)
base = list(data); core = [p for p in base if p not in B.EXTRA]
allowed = {k: (core if U.NOTEBOOK[k].get('universe') == 'core' else base + (list(new) if k == 'P1' else [])) for k in U.NOTEBOOK}
both = {**data, **new}
for split in splits:
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split]); weeks = (b - a).days / 7
    for slots in (6, 8, 10):
        B.SLOTS = slots
        got = U.portfolio(both, split, allowed)
        r = np.array([x[2] for x in got]); eq = np.cumsum(r)
        dd = float(np.min(eq - np.maximum.accumulate(eq)))
        # худший час: сумма R сделок, открытых в один час (один каскад)
        by_t = pd.Series(r, index=[x[0] for x in got]).groupby(level=0).sum()
        print(f'{split:6s} мест {slots:2d}: {len(r):4d} сд {len(r)/weeks:4.1f}/нед сумма {r.sum():+6.1f}R на сделку {r.mean():+.3f} '
              f'просадка {dd:6.1f}R худший час входа {by_t.min():+5.1f}R', flush=True)
        lines.append(f'{split:6s} мест {slots:2d}: {len(r):4d} сд {len(r)/weeks:4.1f}/нед сумма {r.sum():+6.1f}R на сделку {r.mean():+.3f} просадка {dd:6.1f}R худший час входа {by_t.min():+5.1f}R')
with open(os.path.join(HERE, 'results', f'ai_notebook_slots_{mode}.txt'), 'w', encoding='utf-8') as fh:
    fh.write('\n'.join(lines) + '\n')
