"""HOLD — один прогон замороженной V2 (PROTOCOL.md, заморозка 756527e)."""
import sys
import numpy as np, pandas as pd
from fibz import analyze as A
from fibz.broad import TODAY, line
from smcz import sim
KEY = ('12h', 3, 0.382, '786', 'e2618', 12, 120)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    for tag, label in (('s2', 'Bybit 31 пара — ОСНОВНОЙ'), ('bnc', 'Binance 30 пар'), ('bvc', 'широкая выборка')):
        full = A.frame(A.load(tag), KEY)
        if tag == 'bvc':
            sym = full.pair.str.partition('@')[0]
            full = full[((full.adv30 >= 20e6) & (full.age_d >= 90)).to_numpy() & ~sym.isin(TODAY).to_numpy()].reset_index(drop=True)
            label += ' (оборот ≥ 20 млн, без сегодняшних пар)'
        df = A.exclusive(full[full.period == 'HOLD'].reset_index(drop=True))
        f = df[df.status == sim.ST_FILLED]
        print(f'\n{label}')
        print(line(df, 'HOLD'))
        if len(f):
            print(f'    ×1.5 {f.r_net15.mean():+.3f}; выходы {f.ex_type.value_counts().to_dict()}; стоп медиана {f.stop_pct.median()*100:.1f}%')
            q = f.assign(q=pd.to_datetime(f.minute * 60, unit='s').dt.to_period('Q')).groupby('q').r_net.agg(['size', 'mean']).round(3)
            print('    кварталы:', ' '.join(f'{i} {r["mean"]:+.3f}({int(r["size"])})' for i, r in q.iterrows()))
            pp = f.groupby('pair').r_net.mean()
            print(f'    пар в плюсе {(pp > 0).mean():.0%} из {len(pp)}')
        if tag == 's2':
            m = f.r_net.mean()
            print(f'    КРИТЕРИЙ HOLD: среднее R {m:+.3f} → {"> 0 — ПРОШЛА" if m > 0 else "≤ 0 — НЕ ПРОШЛА"}')


if __name__ == '__main__':
    main()
