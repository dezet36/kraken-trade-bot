"""SMC с нуля — первая сетка на периоде разработки (dev). Все варианты пишутся
в results/smc0/grid_dev_1.csv — и провальные тоже (счёт перебора)."""
import itertools, time, sys, os
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smc0_model as M

rows = []
t0 = time.time()
entries = ['mkt', 'half', 'mss_mkt', 'fvg', 'fvg_ce', 'ob', 'ote']
targets = [dict(target='rr', rr=2.0), dict(target='rr', rr=3.0), dict(target='liq', min_rr=2.0, max_rr=6.0)]
filters = {'none': {}, 'deep': dict(depth_min=0.9), 'deep_flush': dict(depth_min=0.9, oi_sweep_max=-0.003)}
for e, (ti, tg), (fn, fl) in itertools.product(entries, enumerate(targets), filters.items()):
    cfg = dict(entry=e, **tg, **fl)
    tr = M.run(cfg, 'dev')
    r = M.report(tr, f"{e}|{['rr2','rr3','liq'][ti]}|{fn}", span_weeks=130.3)
    rows.append(r)
    print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}, flush=True)
pd.DataFrame(rows).to_csv('results/smc0/grid_dev_1.csv', index=False)
print('сек', round(time.time() - t0))
