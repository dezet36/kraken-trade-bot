"""
Переносятся ли закономерности тетради на другие монеты (28.09.2026).

Зачем: владельцу нужно 6–8 сделок в неделю; на падающем рынке тетрадь v2
торгует реже. P2 и B3 — закономерности внутри монеты, их можно проверить на
других ликвидных парах; P1 — по-прежнему по широте разгрузки на исходных 20.

ЗАПИСАНО ДО ЗАМЕРА. 10 пар с историей с 2022–23 (research/flow_cache_extra):
BCH, ETC, ATOM, FIL, TRX, OP, APT, INJ, 1000PEPE, SEI. Закономерность
переносится, если на этих парах R > 0 и в train, и в valid при ≥ 30 сделках
в каждом. test не смотрим: расширение, если пройдёт, подтверждается вживую.
Оговорка: пары выбраны по сегодняшнему обороту (выжившие) — для
закономерностей внутри монеты это допустимо, для «какая монета сильнее» — нет.

    python research/ai_extra_pairs.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_pattern_lab as L                            # noqa: E402
import ai_model_trader_bt as B                        # noqa: E402

EXTRA = os.path.join(HERE, 'flow_cache_extra')


def main():
    base = B.prepare()                                # 20 пар, широта разгрузки по ним
    btc = base['BTCUSDT'][0]
    breadth = base['BTCUSDT'][1]['cascade_count_3h']
    extra = {}
    for name in sorted(os.listdir(EXTRA)):
        if not name.endswith('.pkl'):
            continue
        df = pd.read_pickle(os.path.join(EXTRA, name))
        df = df[df['v'] > 0]
        f = L.features(df, btc)
        f['cascade_count_3h'] = breadth.reindex(f.index).to_numpy()
        extra[name[:-4]] = (df, f)
    print('пары:', ', '.join(extra), flush=True)
    for key, pat in B.PATTERNS.items():
        rule = {'side': pat['side'], 'hold_hours': pat['hold'], 'stop_atr': pat['stop'], 'conditions': pat['conditions']}
        s_new, _ = L.evaluate(rule, data=extra, splits=('train', 'valid'))
        s_old, _ = L.evaluate(rule, data=base, splits=('train', 'valid'))
        ok = all(s_new[x]['n'] >= 30 and s_new[x]['r'] > 0 for x in ('train', 'valid'))
        print(L.line(f'{key} на новых 10', s_new), 'ПЕРЕНОСИТСЯ' if ok else '', flush=True)
        print(L.line(f'{key} на прежних 20', s_old), flush=True)
    # Портфель 30 пар, 6 мест — механика, train и valid
    both = dict(base)
    both.update(extra)
    for split in ('train', 'valid'):
        evs = B.alerts(both, split)
        res = B.outcomes(both, evs)
        a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
        B.summary(f'30 пар {split}', B.play(evs, res, lambda t, c, h, f: [p for p, _ in c]), (b - a).days / 7)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
