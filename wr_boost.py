"""
wr_boost.py — кандидаты на повышение WR (16 пар / cap 5 / v3-база):
- tp18:       TP -18% вместо -25% (известный трейд-офф WR<->PnL, цифры на деплой-пуле)
- ntp2_10_25: 50% позиции на -10% + 50% на -25% (ранний частичный тейк -> сделка
              становится выигрышем даже при откате остатка в безубыток)
- ntp2_14_30: 50% на -14% + 50% на -30% (середина)
Контроль: v3_hold_336 (+220.8% / WR 34.4% / PF 1.278 / DD 21.3%).
"""
import json
import time

import pandas as pd

import backtest as bt
import backtest_campaign as camp
import bt12
from funnel_sweep import (CAMP_BASE, BT_BASE, POOL16, RISK, CAP,
                          bounds, base_row, evaluate, fmt)

CONFIGS = {
    'tp18':       {'cfg': {'ntp': 1}, 'camp': {'TP1_EXT': 0.18}},
    'ntp2_10_25': {'cfg': {'ntp': 2}, 'camp': {'TP1_EXT': 0.10, 'TP1B_EXT': 0.25}},
    'ntp2_14_30': {'cfg': {'ntp': 2}, 'camp': {'TP1_EXT': 0.14, 'TP1B_EXT': 0.30}},
}


def main():
    t0 = time.time()
    data_1h, data_5m = bt12.load(POOL16)
    T0, MID = bounds()
    print(fmt(base_row(T0, MID)) + '   <- контроль (деплой)')
    for name, spec in CONFIGS.items():
        for k, v in CAMP_BASE.items():
            setattr(camp, k, v)
        for k, v in BT_BASE.items():
            setattr(bt, k, v)
        setattr(camp, 'TP1B_EXT', 0.27)
        for k, v in spec['camp'].items():
            setattr(camp, k, v)
        cfg = {'e1b': False, 'e2': False, 'bos': False, 'be': True, **spec['cfg']}
        trd = []
        for p in POOL16:
            trd.extend(bt12.sim_trades(p, data_1h[p], data_5m[p],
                                       data_1h.get(p + '_4h'), cfg, f'wr_{name}'))
        out = {'config': name, **evaluate(trd, T0, MID)}
        with open(rf'D:\Bot trade\exp_wr_{name}.json', 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(fmt(out))
    print(f'Время: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    main()
