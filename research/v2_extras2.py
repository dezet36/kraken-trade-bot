"""
v2_extras2.py — вторая партия доводок v2 (10 пар, cap=4):
- nosess:    v2-геометрия БЕЗ сессионного фильтра (изоляция вклада сессии на v2)
- block_mon: v2 + блок рождения сетапов по понедельникам (пандас: -85R на 356
             сделках, WR 19.9%, обе половины года отрицательны)
- sess_mon:  сессия 12-16 + понедельник вместе
"""
import json
import time

import pandas as pd

import backtest as bt
import backtest_campaign as camp
import bt12
from v2_extras import BASE_CAMP, BT_DEF, evaluate

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}

CONFIGS = {
    'nosess':    {'BLOCK_BIRTH_HOURS': frozenset()},
    'block_mon': {'BLOCK_BIRTH_DOW': frozenset({0})},
    'sess_mon':  {'BLOCK_BIRTH_DOW': frozenset({0})},   # сессия уже в BASE_CAMP
}

def main():
    t0 = time.time()
    data_1h, data_5m = bt12.load(bt12.PAIRS10)
    t_end = max(data_5m[p]['timestamp'].iloc[-1] for p in bt12.PAIRS10).tz_localize(None)
    T0 = t_end - pd.Timedelta(days=bt12.EVAL_DAYS)
    MID = t_end - pd.Timedelta(days=bt12.EVAL_DAYS // 2)

    for name, knobs in CONFIGS.items():
        for k, v in BASE_CAMP.items():
            setattr(camp, k, v)
        for k, v in BT_DEF.items():
            setattr(bt, k, v)
        setattr(camp, 'BLOCK_BIRTH_DOW', frozenset())   # сброс новой ручки
        if name == 'block_mon':
            camp.BLOCK_BIRTH_HOURS = frozenset()        # ЧИСТЫЙ эффект понедельника (без сессии)
        for k, v in knobs.items():
            setattr(camp, k, v)
        trd = []
        for p in bt12.PAIRS10:
            trd.extend(bt12.sim_trades(p, data_1h[p], data_5m[p],
                                       data_1h.get(p + '_4h'), CFG, f'v2x_{name}'))
        out = {'config': name, **evaluate(trd, T0, MID)}
        with open(rf'D:\Bot trade\research\results\exp_v2x_{name}.json', 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"{name:<12} n={out['n']:<5} WR={out['wr']:4.1f}% PF={out['pf_r'] or 0:5.3f} "
              f"PnL={out['pnl_pct']:+7.1f}% DD={out['max_dd_pct']:4.1f}% "
              f"| H1 {out['h1_sumR']:+6.1f} | H2 {out['h2_sumR']:+6.1f}")
    print(f'Время: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    main()
