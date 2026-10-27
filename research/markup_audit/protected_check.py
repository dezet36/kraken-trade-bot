"""
Классическая структура (STRUCTURE_MODE='protected') против своего правила.

Правило переписано здесь заново, симметрично по стороне (d = +1 вверх, −1
вниз), по описанию в docs/протоколе, а не по коду analysis/smc/structure:
  свинги — фрактал 2+2 строго, виден через 2 свечи (elements.my_swings);
  last[d]  — последний опубликованный непробитый свинг стороны d;
  prot[d]  — защищённый свинг тренда d (низ, с которого пошёл импульс к
             последнему BOS вверх; для тренда вниз — верх);
  в тренде T: закрытие за last[T] — BOS T; иначе закрытие за prot[T]
             (нет защищённого — за last[−T]) — CHoCH в −T;
  без тренда: сначала пробой last[+1] вверх, потом last[−1] вниз — CHoCH;
  слом в сторону d: prot[d] = last[−d], prot[−d] = нет, last[d] = нет.
Сравнение — сломы целиком (свеча, тип, сторона, уровень, пробитый свинг) на
1ч и 4ч 10 пар SMC, свечи стенда (кэши bear, mid1, mid2, 12m).

    python research/markup_audit/protected_check.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from elements import my_swings                          # noqa: E402
from structure_quality import PAIRS, frames             # noqa: E402
from analysis.smc import params as P, structure         # noqa: E402

UP, DOWN = 1, -1
NAME = {UP: 'BULLISH', DOWN: 'BEARISH'}


def my_protected(df):
    c = df.close.to_numpy()
    hs, ls = my_swings(df)
    pub = {}
    for i, p, conf in hs:
        pub.setdefault(conf, []).append((UP, i, p))
    for i, p, conf in ls:
        pub.setdefault(conf, []).append((DOWN, i, p))
    last = {UP: None, DOWN: None}
    prot = {UP: None, DOWN: None}
    trend, out = 0, []

    def crossed(point, d, price):
        return point is not None and (price - point[1]) * d > 0

    for k in range(len(c)):
        for d, i, p in sorted(pub.get(k, ()), key=lambda x: x[1]):
            last[d] = (i, p)
        brk = None
        if trend == 0:
            for d in (UP, DOWN):
                if crossed(last[d], d, c[k]):
                    brk = (d, last[d])
                    break
        elif crossed(last[trend], trend, c[k]):
            brk = (trend, last[trend])
        else:
            guard = prot[trend] if prot[trend] is not None else last[-trend]
            if crossed(guard, -trend, c[k]):
                brk = (-trend, guard)
        if brk is None:
            continue
        d, point = brk
        out.append((k, 'BOS' if trend == d else 'CHOCH', NAME[d], point[1], point[0]))
        prot[d], prot[-d], last[d], trend = last[-d], None, None, d
    return out


def bot_protected(df):
    P.STRUCTURE_MODE = 'protected'
    try:
        ev = structure.build_structure(df, tier='swing')['events']
    finally:
        P.STRUCTURE_MODE = 'fractal'
    return [(e['index'], e['type'], e['direction'], e['level'], e['broken_index']) for e in ev]


def main():
    assert P.SWING_N_STRUCT == 2 and P.BREAK_ON_CLOSE, 'правило выше — для фрактала 2 и пробоя закрытием'
    total = bad = 0
    for pair in PAIRS:
        for tf in ('1h', '4h'):
            for part, df in enumerate(frames(pair, tf)):
                a, b = my_protected(df), bot_protected(df)
                total += len(a)
                if a != b:
                    bad += 1
                    k = next((j for j, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
                    print(f'РАСХОЖДЕНИЕ {pair} {tf} часть {part}: моё {len(a)}, бот {len(b)}; первое №{k}:',
                          a[k] if k < len(a) else '—', '|', b[k] if k < len(b) else '—')
        print(pair, 'проверено', flush=True)
    print(f'\nсломов {total}, расхождений по частям: {bad}')
    return bad


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(1 if main() else 0)
