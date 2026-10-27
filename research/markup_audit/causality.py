"""
Аудит разметки 27.10.2026, часть 1 — заглядывание в будущее.

Окна свечей как у бота (analysis/market_structure.load_frames: закрытые
1д × 124, 4ч × 184, 1ч × 304 на момент решения). Окно A — как видит бот на
закрытии часа; окно B — то же начало плюс 48 будущих часов (и закрытые за это
время 4ч и 1д). Решение SMC на той же свече в A и B обязано совпасть целиком:
есть ли сетап, причина отказа, сторона, блок, вход, стоп, цели, подтверждения.
Любое расхождение — разметка использует будущее.

    python research/markup_audit/causality.py [пары…]
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
sys.path.insert(0, RESEARCH)
sys.path.insert(0, os.path.join(os.path.dirname(RESEARCH), 'Live_Bot'))

from smcz import data as D                                   # noqa: E402
from analysis.smc.context import MarketContext               # noqa: E402
from strategies.smc import params as smc_params              # noqa: E402

SIZES = {'bias': (1440, 124), 'htf': (240, 184), 'poi': (60, 304)}
FUTURE_MIN = 48 * 60
STEP_H = 9
FROM, TO = D.minute('2025-10-01'), D.minute('2026-09-25')


def frames_of(pair):
    bars = D.load(pair, tfs=('1h', '4h', '1d'), src='bybit')
    out = {}
    for key, tf in (('bias', '1d'), ('htf', '4h'), ('poi', '1h')):
        B = bars[tf]
        out[key] = (B.t.copy(), pd.DataFrame({'timestamp': pd.to_datetime(B.t * 60, unit='s'),
                                              'open': B.o, 'high': B.h, 'low': B.l, 'close': B.c,
                                              'volume': B.qv}))
    return out


def window(t, df, tf, n, until, extra):
    closed = np.flatnonzero(t + tf <= until)
    if not len(closed):
        return None
    last = closed[-1]
    lo = max(0, last - n + 1)
    hi = last + 1
    if extra:
        more = np.flatnonzero(t + tf <= until + FUTURE_MIN)
        hi = more[-1] + 1
    return df.iloc[lo:hi].reset_index(drop=True)


def key(setup, reason):
    if setup is None:
        return ('нет', reason)
    p = setup['params']
    return ('сетап', setup['direction'], setup['poi']['type'], int(setup['poi']['index']),
            round(p['entry'], 8), round(p['stop_loss'], 8), tuple(round(x, 8) for x in p['targets']),
            round(setup['confluence'], 6), tuple(sorted(k for k, v in setup['factors'].items() if v)))


def main(pairs):
    smc_params.MIN_SL_PCT = 0.008                     # настройка оператора на сервере
    total = same = setups = 0
    diffs = []
    for pair in pairs:
        fr = frames_of(pair)
        t1 = fr['poi'][0]
        for i in range(len(t1)):
            close = int(t1[i] + 60)
            if close < FROM or close > TO or (close // 60) % STEP_H:
                continue
            A = {k: window(fr[k][0], fr[k][1], SIZES[k][0], SIZES[k][1] + 4, close, False) for k in fr}
            B = {k: window(fr[k][0], fr[k][1], SIZES[k][0], SIZES[k][1] + 4, close, True) for k in fr}
            if any(v is None for v in A.values()):
                continue
            at = len(A['poi']) - 1
            ka = key(*MarketContext(A, pair=pair).evaluate(at, decision=smc_params))
            kb = key(*MarketContext(B, pair=pair).evaluate(at, decision=smc_params))
            total += 1
            setups += ka[0] == 'сетап'
            if ka == kb:
                same += 1
            else:
                diffs.append((pair, str(pd.to_datetime(close * 60, unit='s')), ka, kb))
        print(f'{pair}: проверено {total}, совпало {same}, сетапов {setups}', flush=True)
    print(f'\nвсего {total} решений, совпало {same} ({same / max(total, 1):.1%}), сетапов в окне бота {setups}')
    for d in diffs[:20]:
        print('РАСХОЖДЕНИЕ', d[0], d[1], '\n   бот:   ', d[2], '\n   +48ч:  ', d[3])
    return diffs


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1:] or ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT'])
