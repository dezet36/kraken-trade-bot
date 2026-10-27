"""
Аудит разметки 27.10.2026, часть 2 — каждый элемент против своего правила.

Правила переписаны здесь заново, по описаниям в коде бота (analysis/smc/*), и
сравниваются с тем, что выдаёт бот, на годе часовых свечей Bybit:
  свинги      — фрактал 2+2 строго, виден через 2 свечи;
  сломы       — закрытие за последним опубликованным непробитым свингом;
                CHoCH, если тренд был обратным; уровень после слома отработан;
  блоки       — последняя встречная свеча до слома (≤ 20 свечей), ход от неё
                до слома ≥ 0.8%, зона — high/low свечи, подтверждён сломом;
  имбалансы   — гэп трёх свечей шире 0.10% цены, виден на третьей свече.
Плюс описание структуры: как часто меняется тренд и сколько сломов — CHoCH.

    python research/markup_audit/elements.py
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
from analysis.smc import imbalance, poi, structure, swings   # noqa: E402
from analysis.smc import params as P                         # noqa: E402

FROM, TO = D.minute('2025-10-01'), D.minute('2026-09-25')


def frame(pair, tf):
    B = D.load(pair, tfs=(tf,), src='bybit')[tf]
    m = (B.t >= FROM) & (B.t < TO)
    return pd.DataFrame({'timestamp': pd.to_datetime(B.t[m] * 60, unit='s'), 'open': B.o[m],
                         'high': B.h[m], 'low': B.l[m], 'close': B.c[m], 'volume': B.qv[m]}).reset_index(drop=True)


def my_swings(df, n=2):
    h, l = df.high.to_numpy(), df.low.to_numpy()
    hs, ls = [], []
    for i in range(n, len(h) - n):
        if all(h[i] > h[i - j] and h[i] > h[i + j] for j in range(1, n + 1)):
            hs.append((i, h[i], i + n))
        if all(l[i] < l[i - j] and l[i] < l[i + j] for j in range(1, n + 1)):
            ls.append((i, l[i], i + n))
    return hs, ls


def my_events(df, hs, ls):
    c = df.close.to_numpy()
    pub = {}
    for i, p, conf in hs:
        pub.setdefault(conf, []).append(('high', i, p))
    for i, p, conf in ls:
        pub.setdefault(conf, []).append(('low', i, p))
    ref_h = ref_l = None
    trend, out = 0, []
    for k in range(len(c)):
        for kind, i, p in sorted(pub.get(k, []), key=lambda x: (x[1], x[0])):
            if kind == 'high':
                ref_h = (i, p)
            else:
                ref_l = (i, p)
        if ref_h is not None and c[k] > ref_h[1]:
            out.append((k, 1, 'BOS' if trend == 1 else 'CHOCH', ref_h[1]))
            trend, ref_h = 1, None
        elif ref_l is not None and c[k] < ref_l[1]:
            out.append((k, -1, 'BOS' if trend == -1 else 'CHOCH', ref_l[1]))
            trend, ref_l = -1, None
    return out


def my_blocks(df, events):
    o, c, h, l = (df[k].to_numpy() for k in ('open', 'close', 'high', 'low'))
    out = []
    for k, side, _, _ in events:
        ob = None
        for i in range(k - 1, max(0, k - 20) - 1, -1):
            bear = c[i] < o[i]
            if (side == 1 and bear) or (side == -1 and not bear):
                ob = i
                break
        if ob is None:
            continue
        imp = (h[k] - l[ob]) / l[ob] if side == 1 else (h[ob] - l[k]) / h[ob]
        if imp < P.OB_MIN_IMPULSE_PCT or not h[ob] > l[ob]:
            continue
        out.append((ob, k, side, h[ob], l[ob]))
    return out


def my_fvg(df):
    h, l = df.high.to_numpy(), df.low.to_numpy()
    out = []
    for i in range(2, len(h)):
        if l[i] > h[i - 2] and (l[i] - h[i - 2]) / l[i] >= P.FVG_MIN_SIZE_PCT:
            out.append((i, 1))
        if h[i] < l[i - 2] and (l[i - 2] - h[i]) / l[i - 2] >= P.FVG_MIN_SIZE_PCT:
            out.append((i, -1))
    return sorted(out)


def main(pairs=('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT')):
    for tf in ('1h', '4h', '1d'):
        for pair in pairs:
            df = frame(pair, tf)
            hs, ls = swings.find_swings(df, n=P.SWING_N_STRUCT)
            mh, ml = my_swings(df, P.SWING_N_STRUCT)
            ok_sw = ([(s['index'], s['price'], s['confirmed_at']) for s in hs] == mh
                     and [(s['index'], s['price'], s['confirmed_at']) for s in ls] == ml)
            st = structure.build_structure(df, tier='swing')
            bot_ev = [(e['index'], 1 if e['direction'] == 'BULLISH' else -1, e['type'], e['level'])
                      for e in st['events']]
            ev = my_events(df, mh, ml)
            ok_ev = bot_ev == ev
            line = f'{tf:3s} {pair:9s} свечей {len(df):5d} | свинги {"ok" if ok_sw else "РАЗНЫЕ"} | сломы {"ok" if ok_ev else "РАЗНЫЕ"} ({len(ev)})'
            if tf == '1h':
                bot_ob = sorted((b['index'], b['confirmed_at'], 1 if b['direction'] == 'BULLISH' else -1,
                                 b['top'], b['bottom']) for b in poi.find_order_blocks(df, st))
                ok_ob = bot_ob == sorted(my_blocks(df, ev))
                bot_fvg = [(g['index'], 1 if g['direction'] == 'BULLISH' else -1) for g in imbalance.find_fvg(df)]
                ok_fvg = sorted(bot_fvg) == my_fvg(df)
                line += f' | блоки {"ok" if ok_ob else "РАЗНЫЕ"} ({len(bot_ob)}) | имбалансы {"ok" if ok_fvg else "РАЗНЫЕ"} ({len(bot_fvg)})'
            # как ведёт себя «тренд» в таком определении
            kinds = [e[2] for e in ev]
            flips = sum(1 for a, b in zip(ev, ev[1:]) if a[1] != b[1])
            line += f' | CHoCH {kinds.count("CHOCH") / max(len(kinds), 1):.0%}, смена тренда раз в {len(df) / max(flips, 1):.0f} свечей'
            print(line, flush=True)
            if not ok_ev:
                diff = [(a, b) for a, b in zip(bot_ev, ev) if a != b][:3]
                print('   первые расхождения (бот, правило):', diff)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
