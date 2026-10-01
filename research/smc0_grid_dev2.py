"""SMC с нуля — сетка 2 на dev: снятие на 4ч и продолжение (BOS) на 1ч/4ч.
Все варианты пишутся в results/smc0/grid_dev_2.csv."""
import itertools, time, sys, os, warnings
warnings.filterwarnings('ignore')
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smc0_model as M

H4 = dict(tf='4h', k_ext=3, ext_lookback=42, min_age=3, reclaim_bars=1, mss_bars=6,
          expiry=6, max_hold=30, er_bars=42)
H1B = dict(tf='1h', k_bos=3, expiry=12, max_hold=72)
H4B = dict(tf='4h', k_bos=2, expiry=6, max_hold=30, er_bars=42)
targets = {'rr2': dict(target='rr', rr=2.0), 'rr3': dict(target='rr', rr=3.0),
           'liq': dict(target='liq', min_rr=2.0, max_rr=6.0)}
rows = []
t0 = time.time()
def go(label, cfg):
    tr = M.run(cfg, 'dev')
    r = M.report(tr, label, span_weeks=130.3)
    rows.append(r)
    print(label, r.get('n'), round(r.get('per_wk', 0), 2), round(r.get('R/tr', 0), 3),
          [round(r.get('ci_lo', 0), 3), round(r.get('ci_hi', 0), 3)], 'gross', round(r.get('grossR/tr', 0), 3), flush=True)

filters = {'none': {}, 'deep': dict(depth_min=0.9), 'deep_flush': dict(depth_min=0.9, oi_sweep_max=-0.003)}
for e, (tn, tg), (fn, fl) in itertools.product(['mkt', 'mss_mkt', 'fvg', 'ob'], targets.items(), filters.items()):
    go(f"sweep4h|{e}|{tn}|{fn}", dict(H4, entry=e, **tg, **fl))
for (tfn, base), e, (tn, tg), dm in itertools.product([('1h', H1B), ('4h', H4B)], ['fvg', 'ob', 'eq'], targets.items(), [1.5, 3.0]):
    go(f"bos{tfn}|{e}|{tn}|disp{dm}", dict(base, model='bos', entry=e, disp_min=dm, **tg))
pd.DataFrame(rows).to_csv('results/smc0/grid_dev_2.csv', index=False)
print('сек', round(time.time() - t0))
