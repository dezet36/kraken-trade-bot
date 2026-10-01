"""
Разбор SMC, раздел 8А (docs/SMC_разбор_аналитика_2026-10-01.md): насколько
живая SMC зажата — снимаем ворота по одному и все разом. Портфель с правилами
брокера на стенде по всей истории (research/smc_lab_eval: фандинг по факту,
итог без двойного счёта 12m/fresh); свежий период — 20 пар
(results/smcr/rows_base_fresh.pkl, research/smcr_struct.py base).

    python research/smcr_gates.py     # → results/smcr/eval_gates.txt
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smcr_live as L                                 # noqa: E402  (свежий период — fresh20)

E = L.E
ALL20 = tuple(L.D.PAIRS)
YEARS = 4.7                                            # 2022-01 … 2026-09 без повтора


def crowd(r, feats):
    return not ((feats or {}).get('funding', np.nan) > -1.0)


def main():
    fresh = pd.read_pickle(os.path.join(L.OUT, 'rows_base_fresh.pkl')).sort_values(['t', 'pair']).reset_index(drop=True)
    fresh['key'] = list(zip(fresh['pair'], fresh['poi_type'], fresh['poi_index'], fresh['dir']))
    E._rows[('fresh', '1h')] = fresh
    q = dict(min_conf=0.0, min_rr=1.5, min_stop=0.2, cost_limit=0.0)
    variants = [
        ('как сейчас (живая SMC)', {'filt': crowd}, True),
        ('без конфлюенса (порог 0 вместо 4.5)', {'filt': crowd, 'min_conf': 0.0}, False),
        ('без R:R ≥ 4 (от 1.5)', {'filt': crowd, 'min_rr': 1.5}, False),
        ('R:R от 3', {'filt': crowd, 'min_rr': 3.0}, False),
        ('без минимума стопа (от 0.2% вместо 0.8%)', {'filt': crowd, 'min_stop': 0.2}, False),
        ('без предела издержек', {'filt': crowd, 'cost_limit': 0.0}, False),
        ('без кэпа 3 и паузы 12 ч', {'filt': crowd, 'cap': 0, 'cooldown': 0.0}, False),
        ('20 пар вместо 10', {'filt': crowd, 'pairs': ALL20}, False),
        ('без фильтра толпы', {}, True),
        ('сняты пороги качества, толпа есть', dict(q, filt=crowd), True),
        ('сняты пороги качества, 20 пар, толпа есть', dict(q, filt=crowd, pairs=ALL20), False),
        ('снято всё, кроме ядра (без толпы, 20 пар, без кэпа)', dict(q, pairs=ALL20, cap=0, cooldown=0.0), True),
    ]
    print('портфель с правилами брокера; итог без двойного счёта; R на сделку [95%]; сделок в год ≈ n / 4.7')
    for label, change, with_ft in variants:
        res = E.run(change, label)
        print(f'      ≈ {res["n"] / YEARS:.0f} сделок в год', flush=True)
        if with_ft:
            E.run(dict(change, fill_through=0.0005), '   … налив насквозь')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
