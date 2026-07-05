"""
warm_v2q.py — прогрев симуляций v2-геометрии (SL 88.6, TP -25, сессия, MIN_RR=0)
для всех пар ВНЕ PAIRS10 — под переквалификацию пула при v2.
config_key = 'v2_A_sl886_tp25' (тот же, что у 10 старых пар из geometry_grid) —
единый кэш для последующего пул-анализа всех 34 символов.
"""
import time

import backtest as bt
import backtest_campaign as camp
import bt12

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}
SESS = frozenset({12, 13, 14, 15, 16})
KNOBS = {'BLOCK_BIRTH_HOURS': SESS, 'BLOCK_FILL_HOURS': frozenset(),
         'ENTRY_R_E1A': None, 'BE_EXT': 0.0, 'HTF_STRICT': False,
         'DIR_CAP': None, 'MIN_IMPULSE_PCT': 3.0, 'COOLDOWN_H': 12,
         'MIN_RR_E1': 0.0, 'SL_R_E1A': 0.886, 'TRAIL_AFTER_BE_K': None,
         'TP1_EXT': 0.25}
BT_DEF = {'IMP_ANCHOR_SNAP': 0, 'IMP_SOFT_FRACTALS': False,
          'IMP_FRESH_FALLBACK': False, 'IMP_MAX_LEG_RETRACE': None}

for k, v in KNOBS.items():
    setattr(camp, k, v)
for k, v in BT_DEF.items():
    setattr(bt, k, v)

pairs = [p for p in bt12.ALL_PAIRS if p not in bt12.PAIRS10]
t0 = time.time()
for i, pair in enumerate(pairs):
    df1, df5, df4 = bt12.load_pair(pair)
    trd = bt12.sim_trades(pair, df1, df5, df4, CFG, 'v2_A_sl886_tp25')
    del df1, df5, df4
    print(f'[{i+1}/{len(pairs)}] {pair}: {len(trd)} сделок | {(time.time()-t0)/60:.1f} мин')
print(f'Готово: {len(pairs)} пар за {(time.time()-t0)/60:.1f} мин')
