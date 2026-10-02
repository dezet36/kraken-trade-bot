"""Эталон для боевого кода: события и геометрия исследовательского движка
на реальных свечах Bybit 4ч → Live_Bot/tests/data/smcs_golden.json.

tests/test_smcs_core.py требует, чтобы smcs.core повторял их в точности:
торговаться должно ровно то, что измерено.

    python -m smcz.make_live_fixture
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from smcz import data as D
from smcz import events as E
from smcz import structure as S

OUT = os.path.join(os.path.dirname(D.ROOT), 'Live_Bot', 'tests', 'data', 'smcs_golden.json')
PAIRS = ['ETHUSDT', 'COTIUSDT', 'SHIB1000USDT']
N = 600


def main():
    out = {}
    for pair in PAIRS:
        bars = D.load(pair, src='bybit', tfs=('1h', '4h', '12h', '1d'))
        B = bars['4h']
        a, b = len(B) - N, len(B)
        o, h, l, c = (x[a:b].astype(float) for x in (B.o, B.h, B.l, B.c))
        _, ev = S.market_structure(o, h, l, c, 3)
        at = S.atr(h, l, c, 14)
        events = []
        for e in ev:
            i = int(e[S.EV_BAR])
            d = int(e[S.EV_DIR])
            # геометрия — как events.orders (зона 'mkt', цель 8R, буфер 0.1 ATR)
            stop = e[S.EV_OBLO] - 0.1 * at[i] if d == 1 else e[S.EV_OBHI] + 0.1 * at[i]
            ref = c[i]
            risk = (ref - stop) * d
            events.append(dict(bar=i, dir=d, kind='BOS' if e[S.EV_KIND] == 0 else 'CHoCH',
                               level=float(e[S.EV_LEVEL]), org_px=float(e[S.EV_ORGPX]),
                               ext=float(e[S.EV_EXT]), ob_hi=float(e[S.EV_OBHI]),
                               ob_lo=float(e[S.EV_OBLO]), stop=float(stop),
                               target=float(ref + d * 8.0 * risk), atr=float(at[i])))
        out[pair] = dict(t=[int(x) for x in B.t[a:b]], o=o.tolist(), h=h.tolist(),
                         l=l.tolist(), c=c.tolist(), events=events)
        print(pair, len(events), 'событий')
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f)
    print(OUT, os.path.getsize(OUT) // 1024, 'КБ')


if __name__ == '__main__':
    main()
