"""SMC с нуля — финалисты + фильтр толпы живой SMC (лонг при ставке ≤ −1 б.п.,
шорт при ≥ +1 б.п.). Фильтр найден прежним исследованием на 2024–26, поэтому
независимы только dev (2022–24) и y21; val/hold — для картины."""
import sys, os, warnings
warnings.filterwarnings('ignore')
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smc0_model as M, smc0_finalists as FN
rows = []
for per in ['dev', 'y21', 'val', 'hold']:
    raw = {}
    for k, cfg in FN.FINALISTS.items():
        for cr in (None, 'live'):
            raw[(k, cr)] = M.run(dict(cfg, cap=0, crowd=cr), per).assign(model=k)
    for cr in (None, 'live'):
        sets = {k: M.apply_cap(raw[(k, cr)], 6) for k in FN.FINALISTS}
        sets['F4'] = M.apply_cap(pd.concat([raw[('F1', cr)], raw[('F2', cr)]]), 6)
        for k, tr in sets.items():
            if tr.empty or not tr['filled'].any():
                rows.append({'period': per, 'label': k, 'crowd': cr or '-', 'n': 0}); continue
            wk = max(1, (tr.ts.max() - tr.ts.min()).days / 7)
            r = M.report(tr, k, span_weeks=wk); r['period'] = per; r['crowd'] = cr or '-'
            rows.append(r)
d = pd.DataFrame(rows)
d.to_csv('results/smc0/crowd_check.csv', index=False)
pd.set_option('display.width', 220)
print(d[['period', 'label', 'crowd', 'n', 'per_wk', 'R/tr', 'ci_lo', 'ci_hi', 'sumR', 'long', 'short']].round(3).to_string(index=False))
