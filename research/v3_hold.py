"""
v3_hold.py — честный пересчёт v2-геометрии с EOD-учётом + sweep тайм-стопа.

Контекст: без EOD-фикса позиции широкого стопа застревали навечно (6 пар из 34
заблокированы с 2025-10 до конца данных!) и выпадали из учёта. Все прежние
v2-цифры (+107.3%/DD 11.2%) искажены. Здесь: обязательная EOD-запись (уже в
симуляторе) + MAX_HOLD_H ∈ {None, 336ч (14д), 168ч (7д), 72ч (3д)}.

Ключи кэша v3_* — свежие симуляции, старые не переиспользуются.
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
    'v3_hold_none': {'MAX_HOLD_H': None},
    'v3_hold_336':  {'MAX_HOLD_H': 336},
    'v3_hold_168':  {'MAX_HOLD_H': 168},
    'v3_hold_72':   {'MAX_HOLD_H': 72},
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
        setattr(camp, 'BLOCK_BIRTH_DOW', frozenset())
        for k, v in knobs.items():
            setattr(camp, k, v)
        trd = []
        for p in bt12.PAIRS10:
            trd.extend(bt12.sim_trades(p, data_1h[p], data_5m[p],
                                       data_1h.get(p + '_4h'), CFG, name))
        out = {'config': name, **evaluate(trd, T0, MID)}
        n_eod = sum(1 for t in trd if str(t.get('exit_reason', '')).startswith('EOD'))
        n_time = sum(1 for t in trd if str(t.get('exit_reason', '')).startswith('TIME'))
        out['n_eod'], out['n_time'] = n_eod, n_time
        with open(rf'D:\Bot trade\research\results\exp_{name}.json', 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"{name:<14} n={out['n']:<5} WR={out['wr']:4.1f}% PF={out['pf_r'] or 0:5.3f} "
              f"PnL={out['pnl_pct']:+7.1f}% DD={out['max_dd_pct']:4.1f}% "
              f"| H1 {out['h1_sumR']:+6.1f} | H2 {out['h2_sumR']:+6.1f} "
              f"| EOD={n_eod} TIME={n_time}")
    print(f'Время: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    main()
