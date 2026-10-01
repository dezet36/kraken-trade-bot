"""SMC с нуля — финалисты (выбраны на dev до проверки, см.
docs/SMC0_с_нуля_2026-09-30.md) и их прогон на любом отрезке.

python smc0_finalists.py val
python smc0_finalists.py hold y21 other
"""
import os
import sys
import warnings
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smc0_data as D
import smc0_model as M

H4B = dict(tf='4h', k_bos=2, expiry=6, max_hold=30, er_bars=42)
FINALISTS = {
    'F1': dict(entry='mkt', depth_min=0.9, oi_sweep_max=-0.003, target='rr', rr=2.0),
    'F2': dict(H4B, model='bos', entry='fvg', disp_min=1.5, target='liq', min_rr=2.0, max_rr=6.0),
    'F3': dict(model='bos', entry='ob', disp_min=3.0, target='rr', rr=3.0, expiry=12, max_hold=72),
}
COMBOS = {'F4': ('F1', 'F2')}


def run_all(period, syms=None, folders=None):
    out = {}
    raw = {}
    for k, cfg in FINALISTS.items():
        raw[k] = M.run(dict(cfg, cap=0), period, syms, folders).assign(model=k)
        out[k] = M.apply_cap(raw[k], 6)
    for k, parts in COMBOS.items():
        out[k] = M.apply_cap(pd.concat([raw[p] for p in parts]), 6)
    return out


def summarize(trades, period_label):
    rows = []
    for k, tr in trades.items():
        if tr.empty:
            continue
        t0, t1 = tr['ts'].min(), tr['ts'].max()
        weeks = max(1, (t1 - t0).days / 7)
        r = M.report(tr, k, span_weeks=weeks)
        f = tr[tr['filled']]
        mid = t0 + (t1 - t0) / 2
        r['half1'] = f[f['ts'] < mid]['R'].mean()
        r['half2'] = f[f['ts'] >= mid]['R'].mean()
        r['period'] = period_label
        rows.append(r)
    return pd.DataFrame(rows)


if __name__ == '__main__':
    pd.set_option('display.width', 250)
    out_dir = os.path.join(D.BASE, 'results', 'smc0')
    allrows = []
    for per in sys.argv[1:] or ['val']:
        if per == 'other':
            tr = run_all(('2022-01-01', '2026-09-28'), syms=D.other_pairs())
        else:
            tr = run_all(per)
        for k, df in tr.items():
            df.to_pickle(os.path.join(out_dir, f'trades_{k}_{per}.pkl'))
        s = summarize(tr, per)
        allrows.append(s)
        cols = ['period', 'label', 'orders', 'n', 'per_wk', 'fill%', 'win%', 'R/tr', 'ci_lo', 'ci_hi',
                'sumR', 'grossR/tr', 'costR/tr', 'maxDD_R', 'half1', 'half2', 'long', 'short']
        print(s[cols].round(3).to_string(index=False), flush=True)
    pd.concat(allrows).to_csv(os.path.join(out_dir, f"finalists_{'_'.join(sys.argv[1:]) or 'val'}.csv"), index=False)
