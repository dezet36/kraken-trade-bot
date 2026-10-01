"""SMC с нуля — второй круг (после провала финалистов на val): BOS на 12ч и 1д.
6 вариантов, dev и val; пишется в results/smc0/round2_htf.csv."""
import sys, os, warnings
warnings.filterwarnings('ignore')
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smc0_model as M
TF = {'12h': dict(tf='12h', k_bos=2, expiry=4, max_hold=20, er_bars=14),
      '1d': dict(tf='1d', k_bos=2, expiry=3, max_hold=15, er_bars=7)}
V = {'fvg_liq': dict(entry='fvg', target='liq', min_rr=2.0, max_rr=6.0),
     'eq_rr3': dict(entry='eq', target='rr', rr=3.0),
     'ob_rr3': dict(entry='ob', target='rr', rr=3.0)}
rows = []
for tfn, base in TF.items():
    for vn, v in V.items():
        for per, wk in (('dev', 130.3), ('val', 52.1)):
            tr = M.run(dict(base, model='bos', disp_min=1.5, **v), per)
            r = M.report(tr, f'bos{tfn}|{vn}|{per}', span_weeks=wk); rows.append(r)
            print(r['label'], r.get('n'), round(r.get('per_wk', 0), 2), round(r.get('R/tr', 0), 3),
                  [round(r.get('ci_lo', 0), 2), round(r.get('ci_hi', 0), 2)], 'gross', round(r.get('grossR/tr', 0), 3),
                  'stop%', round(r.get('stop%', 0), 2), flush=True)
pd.DataFrame(rows).to_csv('results/smc0/round2_htf.csv', index=False)
