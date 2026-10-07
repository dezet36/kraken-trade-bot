"""Портфель замороженной V2 на Bybit 31 пара: риск 0.5%, предел в одну сторону."""
import sys
import numpy as np, pandas as pd
from fibz import analyze as A
from fibz.hold import KEY
from smcz import portfolio as P, sim


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    full = A.frame(A.load('s2'), KEY)
    df = A.exclusive(full.reset_index(drop=True))
    f = df[df.status == sim.ST_FILLED].copy()
    f['t_in'] = [int(A.minute_t(p)[i]) for p, i in zip(f.pair, f.fill_i)]
    f['t_out'] = [int(A.minute_t(p)[i]) for p, i in zip(f.pair, f.end_i)]
    f['side'] = f.dir
    yrs = (f.t_in.max() - f.t_in.min()) / (1440 * 365.25)
    print(f'сделок {len(f)} за {yrs:.1f} года ({len(f) / yrs:.0f} в год), среднее R {f.r_net.mean():+.3f}, '
          f'длительность медиана {(f.t_out - f.t_in).median() / 1440:.1f} сут')
    for cap in (3, 5, 8, 12, None):
        for per, sub in (('2021–2026', f), ('HOLD', f[f.period == 'HOLD'])):
            taken, c = P.run(sub[['pair', 't_in', 't_out', 'r_net', 'side']], risk=0.005, slots=10 ** 6,
                             max_same_side=cap)
            s = P.summary(c)
            yr = P.by_year(c).to_dict() if per != 'HOLD' else {}
            print(f'  в одну сторону ≤ {cap if cap else "∞":>2}  {per:9s} сделок {len(taken):4d}  '
                  f'в год {s["cagr"]:+.1%}  просадка {s["mdd"]:.1%}  ' + ' '.join(f'{y}:{v:+.0%}' for y, v in yr.items()))


if __name__ == '__main__':
    main()
