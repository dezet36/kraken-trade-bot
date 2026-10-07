"""Приёмка кандидатов V1–V3 на VAL (PROTOCOL.md, пункты 1–4)."""
import sys
import numpy as np, pandas as pd
from fibz import analyze as A
from smcz import sim
CANDS = {'V1': ('1d', 3, 0.382, '786', 3.0, 12, 120),
         'V2': ('12h', 3, 0.382, '786', 'e2618', 12, 120),
         'V3': ('12h', 5, 0.3, '786', 3.0, 12, 120)}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    tag = sys.argv[1] if len(sys.argv) > 1 else 's2'
    period = sys.argv[2] if len(sys.argv) > 2 else 'VAL'
    runs = A.load(tag)
    for name, key in CANDS.items():
        full = A.frame(runs, key)
        out = {}
        for p in ('DEV', period):
            df = A.exclusive(full.query('period == @p').reset_index(drop=True))
            out[p] = df[df.status == sim.ST_FILLED]
        v = out[period]
        df_v = A.exclusive(full.query('period == @period').reset_index(drop=True))
        lo, hi = A.day_ci(df_v)
        st = A.stats(df_v)
        pp = v.groupby('pair').r_net.agg(['size', 'mean'])
        pp10 = pp[pp['size'] >= 10]
        yrs = pd.concat([out['DEV'], v]).groupby('year').r_net.mean()
        c1 = st['mean'] > 0 and (lo > 0 or (st['n'] >= 300 and st['t'] > 2))
        c2 = v.r_net15.mean() > 0
        c3 = (pp10['mean'] > 0).mean() >= 0.55 if len(pp10) else None
        c4 = bool((yrs >= -0.05).all())
        print(f'\n{name} {key}')
        print(f'  {period}: n {st["n"]}, R {st["mean"]:+.3f} [{lo:+.3f}; {hi:+.3f}] t {st["t"]:.2f}; ×1.5 {v.r_net15.mean():+.3f}; '
              f'лонг {v[v.dir==1].r_net.mean():+.3f} ({(v.dir==1).sum()}), шорт {v[v.dir==-1].r_net.mean():+.3f} ({(v.dir==-1).sum()})')
        print(f'  пар в плюсе: {(pp["mean"]>0).mean():.0%} из {len(pp)} (с ≥10 сделками: '
              f'{(pp10["mean"]>0).mean() if len(pp10) else float("nan"):.0%} из {len(pp10)})')
        print('  годы:', ' '.join(f'{y} {r:+.3f}' for y, r in yrs.items()))
        q = v.assign(q=pd.to_datetime(v.minute * 60, unit='s').dt.to_period('Q')).groupby('q').r_net.agg(['size', 'mean']).round(3)
        print('  кварталы:', ' '.join(f'{i} {r["mean"]:+.3f}({int(r["size"])})' for i, r in q.iterrows()))
        print(f'  п.1 {"да" if c1 else "НЕТ"} | п.2 {"да" if c2 else "НЕТ"} | п.3 {c3 if c3 is None else ("да" if c3 else "НЕТ")} | п.4 {"да" if c4 else "НЕТ"}'
              f' → {"ПРОШЁЛ п.1–4" if c1 and c2 and (c3 is None or c3) and c4 else "не прошёл"}')


if __name__ == '__main__':
    main()
