"""
warm_sims.py — прогрев кэша симуляций (D1B_sess) для пар ВНЕ PAIRS10,
чтобы pair_qualify.py потом отработал по кэшу мгновенно. Гоняется параллельно
с walk_forward.py: множества пар не пересекаются — гонок записи pkl нет.
"""
import time

import backtest_campaign as camp
import bt12

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}
SESS = frozenset({12, 13, 14, 15, 16})
DEFAULTS = {'BLOCK_BIRTH_HOURS': SESS, 'BLOCK_FILL_HOURS': frozenset(),
            'ENTRY_R_E1A': None, 'BE_EXT': 0.0, 'HTF_STRICT': False,
            'DIR_CAP': None, 'TP1_EXT': 0.18, 'MIN_IMPULSE_PCT': 3.0, 'COOLDOWN_H': 12}

for k, v in DEFAULTS.items():
    setattr(camp, k, v)

pairs = [p for p in bt12.ALL_PAIRS if p not in bt12.PAIRS10]
t0 = time.time()
for i, pair in enumerate(pairs):
    df1, df5, df4 = bt12.load_pair(pair)
    trd = bt12.sim_trades(pair, df1, df5, df4, CFG, 'D1B_sess')
    del df1, df5, df4
    print(f'[{i+1}/{len(pairs)}] {pair}: {len(trd)} сделок | {(time.time()-t0)/60:.1f} мин')
print(f'Готово: {len(pairs)} пар за {(time.time()-t0)/60:.1f} мин')
