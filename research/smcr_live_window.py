"""
Стенд «глазами бота» (research/smc_lab_live._job) для выбранных периодов — в
СВОЮ папку results/smcr/rows_livefix_<период>.pkl (общие строки стенда не
трогаются). Нужен, чтобы сравнить решения SMC до и после исправления уровней
прошлой недели/месяца (01.10.2026) на окне живого бота.

    python research/smcr_live_window.py mid2 fresh
"""
import os
import sys
from multiprocessing import Pool

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OUT = os.path.join(HERE, 'results', 'smcr')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    import ai_doctrine as D
    import backtest_smc as bt
    import smc_lab_live as LL
    periods = sys.argv[1:] or ['mid2', 'fresh']
    jobs = [(p, D.PERIODS[p], pair) for p in periods for pair in bt.DEFAULT_PAIRS]
    acc = {p: [] for p in periods}
    with Pool(4) as pool:
        for period, pair, rows, sec in pool.imap_unordered(LL._job, jobs):
            acc[period] += rows
            print(f'  {period:6s} {pair:13s} {len(rows):6d}  {sec:6.1f} с', flush=True)
    for period, rows in acc.items():
        path = os.path.join(OUT, f'rows_livefix_{period}.pkl')
        pd.DataFrame(rows).to_pickle(path + '.tmp')
        os.replace(path + '.tmp', path)
        print(f'{period}: {len(rows)} → {path}', flush=True)
