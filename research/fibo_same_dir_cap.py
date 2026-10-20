"""
ФИБО: предел позиций и заявок в одну сторону — K = 2, 3, 4, 5 против базы без
предела. Протокол — docs/ФИБО_предел_стороны_2026-10-20.md (закоммичен до
расчёта). Стенд — как research/fibo_old_ideas.py: 9 ячеек, правила брокера
как в боте, налив касанием и «насквозь» 0.05%.

    python research/fibo_same_dir_cap.py   → results/fibo_same_dir_cap.txt
"""
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
import fibo_live_sim as FS                            # noqa: E402
import smc_engine                                     # noqa: E402
from common import ci                                 # noqa: E402
from fibo_crowd import funding_at                     # noqa: E402
from fibo_geometry import CELLS, load_cell            # noqa: E402
from fibo_htf_split import BASE                       # noqa: E402

CAPS = (0, 2, 3, 4, 5)
THROUGH = (0.0, 0.0005)
OUT = os.path.join(HERE, 'results', 'fibo_same_dir_cap.txt')


def naive(x):
    t = pd.Timestamp(x)
    return t.tz_convert('UTC').tz_localize(None) if t.tzinfo else t


def crowd_flags(cache, orders, pairs):
    fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in pairs}
    flags = []
    for o in orders:
        d = 1 if o.direction == 'LONG' else -1
        tms = int(naive(o.created).value // 10 ** 6)
        flags.append(bool(funding_at(fund.get(o.pair), tms) * d <= 0))
    return flags


def cluster_rank(orders):
    """Номер заявки среди заявок той же стороны за предыдущие 2 ч (1 — первая)."""
    t = np.array([np.datetime64(naive(o.created)) for o in orders])
    d = np.array([o.direction for o in orders])
    rank = []
    for i in range(len(orders)):
        same = (d == d[i]) & (t <= t[i]) & (t > t[i] - np.timedelta64(2, 'h'))
        rank.append(int(same.sum()))
    return rank


def drawdown(r):
    if not len(r):
        return 0.0
    cum = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return float((peak - cum).max())


def run_cell(cell):
    cache, orders, pairs = load_cell(cell)
    orders = [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT]
    bt.CACHE_DIR = os.path.join(HERE, cache)
    exec_data = {p: d['5m'] for p in pairs if (d := bt.load_pair(p)) is not None}
    flags = crowd_flags(cache, orders, pairs)
    bases = {'обе, против толпы (как в боте)': [o for o, f in zip(orders, flags) if f],
             'обе, без толпы': list(orders)}
    out = {}
    for base, pool in bases.items():
        pool = sorted(pool, key=lambda o: naive(o.created))
        for cap in CAPS:
            for through in THROUGH:
                smc_engine.FILL_THROUGH_PCT = through
                try:
                    res = smc_engine.run_portfolio(pool, exec_data, **dict(BASE, max_same_direction=cap))
                finally:
                    smc_engine.FILL_THROUGH_PCT = 0.0
                trades = sorted(res['trades'], key=lambda x: x['exit_time'])
                r = np.array([x['pnl'] / x['risk'] for x in trades])
                out[(base, cap, through)] = {'r': r, 'dd': drawdown(r)}
                if cap == 0 and through == 0.0:
                    # Для понимания: R сделок, бывших пятой и дальше в одну сторону за 2 ч
                    # (по времени входа — как в живом разборе 20.10).
                    t = np.array([np.datetime64(naive(x['entry_time'])) for x in trades])
                    d = np.array([x.get('direction') for x in trades])
                    ranks = [int(((d == d[i]) & (t <= t[i]) & (t > t[i] - np.timedelta64(2, 'h'))).sum())
                             for i in range(len(trades))]
                    out[(base, 'rank')] = (r, ranks)
    print(f'   {cell}: готово', flush=True)
    return cell, out


def mean(a):
    return float(a.mean()) if len(a) else float('nan')


def main():
    with Pool(min(len(CELLS), 5)) as pool:
        res = dict(pool.imap_unordered(run_cell, CELLS))
    lines = ['ФИБО: предел позиций и заявок в одну сторону (протокол docs/ФИБО_предел_стороны_2026-10-20.md)', '']
    head = ' '.join(f'{g[:3]}-{p:4s}' for g, p in CELLS)
    for base in ('обе, против толпы (как в боте)', 'обе, без толпы'):
        lines += [f'== база: {base}', f'{"":22s} {head}']
        for cap in CAPS:
            name = 'без предела' if cap == 0 else f'предел {cap}'
            rr = ' '.join(f'{mean(res[c][(base, cap, 0.0)]["r"]):+.3f}' for c in CELLS)
            dd = ' '.join(f'{res[c][(base, cap, 0.0)]["dd"]:7.1f}' for c in CELLS)
            n = ' '.join(f'{len(res[c][(base, cap, 0.0)]["r"]):7d}' for c in CELLS)
            a = np.concatenate([res[c][(base, cap, 0.0)]['r'] for c in CELLS])
            t = np.concatenate([res[c][(base, cap, 0.0005)]['r'] for c in CELLS])
            lo, hi = ci(a)
            lines += [f'{name:22s} R/сд {rr}', f'{"":22s} просадка R {dd}', f'{"":22s} сделок {n}',
                      f'{"":22s} всего {len(a)} сд, {mean(a):+.3f} [{lo:+.3f}; {hi:+.3f}], сумма {a.sum():+.1f}R | '
                      f'насквозь {mean(t):+.3f}, сумма {t.sum():+.1f}R']
        lines.append('')
        if base.startswith('обе, против'):
            lines.append('Приёмка (против базы без предела): просадка меньше в ≥ 7 из 9 ячеек; R/сд всех ячеек не хуже '
                         'на 0.01 и касанием, и насквозь.')
            b0 = np.concatenate([res[c][(base, 0, 0.0)]['r'] for c in CELLS])
            b0t = np.concatenate([res[c][(base, 0, 0.0005)]['r'] for c in CELLS])
            passed = []
            for cap in CAPS[1:]:
                less_dd = sum(res[c][(base, cap, 0.0)]['dd'] < res[c][(base, 0, 0.0)]['dd'] for c in CELLS)
                a = np.concatenate([res[c][(base, cap, 0.0)]['r'] for c in CELLS])
                t = np.concatenate([res[c][(base, cap, 0.0005)]['r'] for c in CELLS])
                ok1 = less_dd >= 7
                ok2 = mean(a) >= mean(b0) - 0.01 and mean(t) >= mean(b0t) - 0.01
                if ok1 and ok2:
                    passed.append(cap)
                lines.append(f'  предел {cap}: просадка меньше в {less_dd}/9 {"✓" if ok1 else "✗"}; R/сд {mean(a):+.3f} '
                             f'против {mean(b0):+.3f}, насквозь {mean(t):+.3f} против {mean(b0t):+.3f} {"✓" if ok2 else "✗"}')
            lines.append(f'  итог: {"принят предел " + str(max(passed)) if passed else "не принят ни один"}')
            lines.append('')
            # Для понимания: пачки на истории
            r_all, rk_all = [], []
            for c in CELLS:
                r, ranks = res[c][(base, 'rank')]
                for x, k in zip(r, ranks):
                    if k is not None:
                        r_all.append(x)
                        rk_all.append(k)
            r_all, rk_all = np.array(r_all), np.array(rk_all)
            if len(rk_all):
                for lo_k, hi_k, label in ((1, 1, 'первая в стороне за 2 ч'), (2, 4, 'вторая–четвёртая'), (5, 99, 'пятая и дальше')):
                    m = (rk_all >= lo_k) & (rk_all <= hi_k)
                    lines.append(f'  {label:26s} {m.sum():5d} сд, {mean(r_all[m]):+.3f} R/сд')
            else:
                lines.append('  (сопоставить сделки с заявками не вышло — поля created в результате нет)')
            lines.append('')
    text = '\n'.join(lines)
    with open(OUT, 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
