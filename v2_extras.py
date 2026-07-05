"""
v2_extras.py — быстрые доводки поверх задеплоенной v2-геометрии (10 пар, cap=4):
- be_off:       нужен ли вообще безубыток при широком стопе? (be=False)
- be_early236:  BE взводится раньше — на уровне 23.6% коррекции (до пробоя B)
- ntp2_25_50:   частичный TP: 50% на -25% (проверенный) + 50% на -50% (хвост бежит)
Контроль: v2-база (кэш v2_A_sl886_tp25) = +107.3% / DD 11.2% / WR 35.7%.
"""
import json
import os
import pickle
import time

import pandas as pd

import backtest as bt
import backtest_campaign as camp
import bt12

RISK, CAP = 0.5, 4
SESS = frozenset({12, 13, 14, 15, 16})
BASE_CAMP = {'BLOCK_BIRTH_HOURS': SESS, 'BLOCK_FILL_HOURS': frozenset(),
             'ENTRY_R_E1A': None, 'BE_EXT': 0.0, 'HTF_STRICT': False,
             'DIR_CAP': None, 'MIN_IMPULSE_PCT': 3.0, 'COOLDOWN_H': 12,
             'MIN_RR_E1': 0.0, 'SL_R_E1A': 0.886, 'TRAIL_AFTER_BE_K': None,
             'TP1_EXT': 0.25, 'TP1B_EXT': 0.27}
BT_DEF = {'IMP_ANCHOR_SNAP': 0, 'IMP_SOFT_FRACTALS': False,
          'IMP_FRESH_FALLBACK': False, 'IMP_MAX_LEG_RETRACE': None}

CONFIGS = {
    'be_off':      {'cfg': {'ntp': 1, 'be': False}, 'knobs': {}},
    'be_early236': {'cfg': {'ntp': 1, 'be': True},  'knobs': {'BE_EXT': -0.236}},
    'ntp2_25_50':  {'cfg': {'ntp': 2, 'be': True},  'knobs': {'TP1B_EXT': 0.50}},
}


def flat(trades):
    if not trades:
        return dict(n=0, sumR=0.0, pf_r=None, wr=0.0)
    rs = [t['R'] for t in trades]
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    return dict(n=len(rs), sumR=round(sum(rs), 1),
                pf_r=round(gp / gl, 3) if gl > 0 else None,
                wr=round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1))


def evaluate(trd, T0, MID):
    trd = [t for t in trd if pd.Timestamp(t['entry_time']) >= T0]
    capped, _, _ = camp.portfolio_filter(trd, cap=CAP)
    capped, curve, ruined = camp.build_equity(capped, risk_pct=RISK)
    fm = flat(capped)
    pnl = (curve[-1] - curve[0]) / curve[0] * 100 if capped else 0.0
    peak = curve[0]; mdd = 0.0
    for b in curve:
        peak = max(peak, b)
        mdd = max(mdd, (peak - b) / peak * 100)
    h1 = flat([t for t in capped if pd.Timestamp(t['entry_time']) < MID])
    h2 = flat([t for t in capped if pd.Timestamp(t['entry_time']) >= MID])
    return {**fm, 'pnl_pct': round(pnl, 1), 'max_dd_pct': round(mdd, 1), 'ruined': ruined,
            'h1_sumR': h1['sumR'], 'h2_sumR': h2['sumR']}


def main():
    t0 = time.time()
    data_1h, data_5m = bt12.load(bt12.PAIRS10)
    t_end = max(data_5m[p]['timestamp'].iloc[-1] for p in bt12.PAIRS10).tz_localize(None)
    T0 = t_end - pd.Timedelta(days=bt12.EVAL_DAYS)
    MID = t_end - pd.Timedelta(days=bt12.EVAL_DAYS // 2)

    base_trd = []
    for p in bt12.PAIRS10:
        with open(os.path.join(bt12.CACHE_12M, 'trades', f'v2_A_sl886_tp25__{p}.pkl'), 'rb') as f:
            base_trd.extend(pickle.load(f))
    b = evaluate(base_trd, T0, MID)
    print(f"v2_base        n={b['n']:<5} WR={b['wr']:4.1f}% PF={b['pf_r'] or 0:5.3f} "
          f"PnL={b['pnl_pct']:+7.1f}% DD={b['max_dd_pct']:4.1f}% | H1 {b['h1_sumR']:+6.1f} | H2 {b['h2_sumR']:+6.1f}")

    for name, spec in CONFIGS.items():
        for k, v in BASE_CAMP.items():
            setattr(camp, k, v)
        for k, v in BT_DEF.items():
            setattr(bt, k, v)
        for k, v in spec['knobs'].items():
            setattr(camp, k, v)
        cfg = {'e1b': False, 'e2': False, 'bos': False, **spec['cfg']}
        trd = []
        for p in bt12.PAIRS10:
            trd.extend(bt12.sim_trades(p, data_1h[p], data_5m[p],
                                       data_1h.get(p + '_4h'), cfg, f'v2x_{name}'))
        out = {'config': name, **evaluate(trd, T0, MID)}
        with open(rf'D:\Bot trade\exp_v2x_{name}.json', 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"{name:<14} n={out['n']:<5} WR={out['wr']:4.1f}% PF={out['pf_r'] or 0:5.3f} "
              f"PnL={out['pnl_pct']:+7.1f}% DD={out['max_dd_pct']:4.1f}% "
              f"| H1 {out['h1_sumR']:+6.1f} | H2 {out['h2_sumR']:+6.1f}")
    print(f'Время: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    main()
