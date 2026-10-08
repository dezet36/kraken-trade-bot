"""Эталон для боевого кода: ноги и геометрия V2 исследовательского движка на
реальных свечах Bybit 12ч → Live_Bot/tests/data/fib12_golden.json.

tests/test_fib12_core.py требует, чтобы fib12.core повторял их на окне,
каким его видит бот (последние HISTORY_BARS закрытых баров).

    python -m fibz.make_live_fixture
"""
from __future__ import annotations

import json
import os

import numpy as np

from smcz import data as D
from smcz import structure as S
from fibz import legs as LG

OUT = os.path.join(os.path.dirname(D.ROOT), 'Live_Bot', 'tests', 'data', 'fib12_golden.json')
PAIRS = ['ETHUSDT', 'COTIUSDT', 'SHIB1000USDT', 'SOLUSDT']
N = 900          # баров в эталоне
CHECK = 350      # из них последние — проверяемые события (окно 500 перед каждым)


def main():
    out = {}
    for pair in PAIRS:
        bars = D.load(pair, src='bybit', tfs=('4h', '12h', '1d'))
        B = bars['12h']
        h, l, c = B.h, B.l, B.c
        ph, pl = S.pivots(h, l, 3)
        raw = LG._legs(h, l, ph, pl, 3)
        at = S.atr(h, l, c, 14)
        a0 = len(B) - N
        legs = []
        for d, a, b, conf, A, Bp, _ in raw:
            d, a, b, conf = int(d), int(a), int(b), int(conf)
            if conf < len(B) - CHECK:
                continue
            L = (Bp - A) * d
            entry = Bp - d * 0.382 * L
            stop = Bp - d * 0.786 * L - d * 0.1 * at[conf]
            target = Bp + d * 1.618 * L
            valid = bool((c[conf] - entry) * d > 0 and (entry - stop) * d > 0)
            legs.append(dict(dir=d, a=a - a0, b=b - a0, conf=conf - a0, A=float(A), B=float(Bp),
                             entry=float(entry), stop=float(stop), target=float(target),
                             atr=float(at[conf]), valid=valid))
        out[pair] = dict(t=[int(x) for x in B.t[a0:]], o=B.o[a0:].tolist(), h=h[a0:].tolist(),
                         l=l[a0:].tolist(), c=c[a0:].tolist(), legs=legs)
        print(pair, len(legs), 'ног, из них с сетапом', sum(g['valid'] for g in legs))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f)
    print(OUT, os.path.getsize(OUT) // 1024, 'КБ')


if __name__ == '__main__':
    main()
