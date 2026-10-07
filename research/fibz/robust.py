"""Устойчивость кандидатов на DEV: соседи, годы, стороны, доля пар в плюсе."""
import sys
import numpy as np, pandas as pd
from fibz import analyze as A
from smcz import sim
sys.stdout.reconfigure(encoding='utf-8')
pd.set_option('display.width', 250)
runs = A.load(sys.argv[1])
period = sys.argv[2] if len(sys.argv) > 2 else 'DEV'
cands = [('12h', 3, 0.382, '786', 'e2618', 12, 120), ('1d', 3, 0.382, '786', 3.0, 12, 120),
         ('12h', 3, 0.382, '786', 3.0, 12, 120), ('1d', 3, 0.382, '786', 'e2618', 12, 120)]
for key in cands:
    df = A.exclusive(A.frame(runs, key).query('period == @period').reset_index(drop=True))
    f = df[df.status == sim.ST_FILLED]
    lo, hi = A.day_ci(df)
    st = A.stats(df)
    yr = f.groupby('year').r_net.agg(['size', 'mean']).round(3)
    pp = f.groupby('pair').r_net.agg(['size', 'mean'])
    pp = pp[pp['size'] >= 10]
    print(f'\n{key}: n {st["n"]} R {st["mean"]:+.3f} t {st["t"]:.2f} [{lo:+.3f}; {hi:+.3f}] '
          f'×1.5 {f.r_net15.mean():+.3f} | лонг {f[f.dir==1].r_net.mean():+.3f} ({(f.dir==1).sum()}) '
          f'шорт {f[f.dir==-1].r_net.mean():+.3f} ({(f.dir==-1).sum()}) | пар в плюсе {(pp["mean"]>0).mean():.0%} из {len(pp)}')
    print('   по годам:', ' '.join(f'{y}: {r["mean"]:+.3f} ({int(r["size"])})' for y, r in yr.iterrows()))
    print('   выходы:', f.ex_type.value_counts().to_dict(), 'стоп %', round(float(f.stop_pct.median()*100), 2))
    # соседи
    tf, k, r, stp, tg, E, H = key
    nb = []
    for kk in (2, 3, 5):
        for rr in (0.3, 0.382, 0.5):
            for t2 in ('e1618', 'e2618', 2.0, 3.0):
                kx = (tf, kk, rr, stp, t2, E, H)
                d2 = A.frame(runs, kx)
                if d2.empty: continue
                d2 = A.exclusive(d2.query('period == @period').reset_index(drop=True))
                nb.append(dict(k=kk, r=rr, target=str(t2), R=A.stats(d2)['mean']))
    nb = pd.DataFrame(nb).pivot_table(index=['k', 'r'], columns='target', values='R').round(3)
    print('   соседи (R на сделку):\n' + nb.to_string())
