"""
det_ab.py — A/B детекции импульса v2.1 на задеплоенной геометрии v2
(SL 88.6% + буфер, TP -25%, безубыток на B, сессия 12-16 UTC, cap=4, risk 0.5%).

Контроль det_base = та же геометрия с v1-детекцией (кэш v2_A_sl886_tp25,
пере-фильтрованный на cap=4 — без ре-симуляции).

Запуск партии:  python det_ab.py <cfg1> <cfg2> ...
Сводка:         python det_ab.py --report
"""
import json
import os
import pickle
import sys
import time

import pandas as pd

import backtest as bt
import backtest_campaign as camp
import bt12

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}
RISK, CAP = 0.5, 4
SESS = frozenset({12, 13, 14, 15, 16})

CAMP_KNOBS = {'BLOCK_BIRTH_HOURS': SESS, 'BLOCK_FILL_HOURS': frozenset(),
              'ENTRY_R_E1A': None, 'BE_EXT': 0.0, 'HTF_STRICT': False,
              'DIR_CAP': None, 'MIN_IMPULSE_PCT': 3.0, 'COOLDOWN_H': 12,
              'MIN_RR_E1': 0.0, 'SL_R_E1A': 0.886, 'TRAIL_AFTER_BE_K': None,
              'TP1_EXT': 0.25}
BT_DEFAULTS = {'IMP_ANCHOR_SNAP': 0, 'IMP_SOFT_FRACTALS': False,
               'IMP_FRESH_FALLBACK': False, 'IMP_MAX_LEG_RETRACE': None}

CONFIGS = {
    'det_snap3':    {'IMP_ANCHOR_SNAP': 3},
    'det_soft':     {'IMP_SOFT_FRACTALS': True},
    'det_freshfb':  {'IMP_FRESH_FALLBACK': True},
    'det_purity60': {'IMP_MAX_LEG_RETRACE': 0.6},
    'det_all':      {'IMP_ANCHOR_SNAP': 3, 'IMP_SOFT_FRACTALS': True,
                     'IMP_FRESH_FALLBACK': True, 'IMP_MAX_LEG_RETRACE': 0.6},
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


def bounds(data_5m):
    t_end = max(data_5m[p]['timestamp'].iloc[-1] for p in bt12.PAIRS10).tz_localize(None)
    return t_end - pd.Timedelta(days=bt12.EVAL_DAYS), t_end - pd.Timedelta(days=bt12.EVAL_DAYS // 2)


def run_one(name, data_1h, data_5m, T0, MID):
    for k, v in CAMP_KNOBS.items():
        setattr(camp, k, v)
    for k, v in BT_DEFAULTS.items():
        setattr(bt, k, v)
    for k, v in CONFIGS[name].items():
        setattr(bt, k, v)

    trd = []
    for p in bt12.PAIRS10:
        trd.extend(bt12.sim_trades(p, data_1h[p], data_5m[p],
                                   data_1h.get(p + '_4h'), CFG, f'v21_{name}'))
    out = {'config': name, 'knobs': CONFIGS[name], **evaluate(trd, T0, MID)}
    with open(rf'D:\Bot trade\research\results\exp_v21_{name}.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"{name:<14} n={out['n']:<5} WR={out['wr']:4.1f}% PF={out['pf_r'] or 0:5.3f} "
          f"PnL={out['pnl_pct']:+7.1f}% DD={out['max_dd_pct']:4.1f}% "
          f"| H1 {out['h1_sumR']:+7.1f} | H2 {out['h2_sumR']:+7.1f}")
    return out


def base_row(T0, MID):
    """Контроль: v1-детекция на той же геометрии (кэш v2_A_sl886_tp25, cap=4)."""
    trd = []
    for p in bt12.PAIRS10:
        with open(os.path.join(bt12.CACHE_12M, 'trades', f'v2_A_sl886_tp25__{p}.pkl'), 'rb') as f:
            trd.extend(pickle.load(f))
    return {'config': 'det_base(v1)', **evaluate(trd, T0, MID)}


def report():
    _, data_5m = bt12.load(bt12.PAIRS10)
    T0, MID = bounds(data_5m)
    rows = [base_row(T0, MID)]
    for name in CONFIGS:
        f = rf'D:\Bot trade\research\results\exp_v21_{name}.json'
        if os.path.exists(f):
            rows.append(json.load(open(f, encoding='utf-8')))
    df = pd.DataFrame(rows)
    cols = ['config', 'n', 'wr', 'pf_r', 'pnl_pct', 'max_dd_pct', 'h1_sumR', 'h2_sumR']
    print(df[cols].to_string(index=False))
    df.to_csv(r'D:\Bot trade\research\results\backtest_v21_det.csv', index=False)
    print('\nСохранено: backtest_v21_det.csv')


def main():
    if '--report' in sys.argv:
        report()
        return
    names = [a for a in sys.argv[1:] if a in CONFIGS] or list(CONFIGS)
    t0 = time.time()
    data_1h, data_5m = bt12.load(bt12.PAIRS10)
    T0, MID = bounds(data_5m)
    print('Контроль (v1-детекция, та же геометрия):')
    b = base_row(T0, MID)
    print(f"{b['config']:<14} n={b['n']:<5} WR={b['wr']:4.1f}% PF={b['pf_r'] or 0:5.3f} "
          f"PnL={b['pnl_pct']:+7.1f}% DD={b['max_dd_pct']:4.1f}% "
          f"| H1 {b['h1_sumR']:+7.1f} | H2 {b['h2_sumR']:+7.1f}")
    for name in names:
        run_one(name, data_1h, data_5m, T0, MID)
    print(f'Партия из {len(names)}: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    main()
