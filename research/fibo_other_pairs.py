"""
Фибо на остальных 10 парах боевого списка — точная симуляция (01.10.2026).

ЗАЧЕМ. Точная симуляция (fibo_live_sim.py) и фильтр толпы (fibo_crowd.py)
мерились на 10 парах backtest_smc.DEFAULT_PAIRS, а живая фибо торгует весь
список бота: с 18.09 54 из 75 её сделок — на парах, которых в замере не было.
Здесь те же заявки боевого сканера на этих парах и те же правила (F1–F5 из
fibo_crowd.py, налив касанием и «насквозь»). F3 «шорты против толпы» — то, что
сейчас торгует бот (стороны оператора = short, порог 0 б.п.); на этих парах
она проверяется впервые, то есть на независимых данных.

    python research/fibo_other_pairs.py gen    # заявки → results/fibo_other/<кэш>_<пара>.pkl (можно прерывать)
    python research/fibo_other_pairs.py eval   # итог → печать (results/fibo_other_pairs.txt)
"""
import os
import pickle
import sys
import time
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fibo_live_sim as FS                            # noqa: E402  (папка данных sim-data, тихий логгер)

OTHER = ('ZECUSDT', 'SUIUSDT', 'ARBUSDT', 'DOTUSDT', 'XLMUSDT', 'SHIB1000USDT', 'NEARUSDT', 'UNIUSDT',
         'AAVEUSDT', 'COTIUSDT')
CACHES = {'bear': 'backtest_cache_bear', 'mid1': 'backtest_cache_mid1', 'mid2': 'backtest_cache_mid2',
          '12m': 'backtest_cache_12m'}
OUT = os.path.join(HERE, 'results', 'fibo_other')


def _path(cache, pair):
    return os.path.join(OUT, f'{cache}_{pair}.pkl')


def _job(task):
    import config
    config.FIBO_FUNDING_AGAINST_CROWD = False           # толпа — фильтром при оценке, как в fibo_crowd.py
    started = time.time()
    cache, pair, orders = FS.orders_for_pair(task)
    with open(_path(cache, pair) + '.tmp', 'wb') as fh:
        pickle.dump(orders, fh)
    os.replace(_path(cache, pair) + '.tmp', _path(cache, pair))
    return f'   {cache} {pair}: {len(orders)} заявок, {(time.time() - started) / 60:.0f} мин'


def generate(procs):
    os.makedirs(OUT, exist_ok=True)
    tasks = [(cache, p) for cache in CACHES.values() for p in OTHER
             if os.path.exists(os.path.join(HERE, cache, f'{p}_5m.pkl')) and not os.path.exists(_path(cache, p))]
    print(f'пар-периодов в работе: {len(tasks)}', flush=True)
    with Pool(procs) as pool:
        for line in pool.imap_unordered(_job, tasks):
            print(line, flush=True)


def evaluate():
    import numpy as np
    import pandas as pd
    import ai_setups as S
    import backtest_smc as bt
    import smc_engine
    from strategies import strategy_profile
    from common import ci
    from fibo_crowd import funding_at
    base = dict(risk_pct=bt.RISK_PCT, max_positions=99, cooldown_hours=strategy_profile.cooldown_hours('FIBO'),
                breakeven_after_tp1=False, max_hold_hours=strategy_profile.max_hold_hours('FIBO') or 336.0,
                max_same_direction=0, occupy_while_pending=True, cooldown_from_placement=True,
                cancel_at_target=True)
    rules = [
        ('живая фибо (обе стороны, без толпы)', lambda o, f: True),
        ('F1 против толпы', lambda o, f: f <= 0),
        ('F2 только шорты', lambda o, f: o.direction == 'SHORT'),
        ('F3 шорты против толпы — КАК В БОТЕ', lambda o, f: o.direction == 'SHORT' and f <= 0),
        ('F4 шорты при ставке ≥ +1 б.п.', lambda o, f: o.direction == 'SHORT' and not (f > -1.0)),
        ('F5 против толпы от середины, обе', lambda o, f: not (f > -1.0)),
        ('лонги (для сравнения)', lambda o, f: o.direction == 'LONG'),
    ]
    totals = {}
    for period, cache in CACHES.items():
        pairs = [p for p in OTHER if os.path.exists(_path(cache, p))]
        if not pairs:
            continue
        orders = []
        for p in pairs:
            with open(_path(cache, p), 'rb') as fh:
                orders += pickle.load(fh)
        orders = [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT]
        bt.CACHE_DIR = os.path.join(HERE, cache)
        exec_data = {p: d['5m'] for p in pairs if (d := bt.load_pair(p)) is not None}
        fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in pairs}
        signed = {}
        for o in orders:
            t = int(pd.Timestamp(o.created).value // 10 ** 6)
            signed[id(o)] = funding_at(fund.get(o.pair), t) * (1.0 if o.direction == 'LONG' else -1.0)
        no_fund = sum(1 for o in orders if not np.isfinite(signed[id(o)]))
        cells = []
        for name, rule in rules:
            for through in (0.0, 0.0005):
                pool_ = [o for o in orders if rule(o, signed[id(o)])]
                smc_engine.FILL_THROUGH_PCT = through
                try:
                    out = run(pool_, exec_data, base)
                finally:
                    smc_engine.FILL_THROUGH_PCT = 0.0
                rs = [t['pnl'] / t['risk'] for t in out['trades']]
                key = name + (', налив насквозь' if through else '')
                totals.setdefault(key, {})[period] = (len(rs), float(np.sum(rs)),
                                                     smc_engine.compute_stats(out).get('max_dd_pct', 0.0), rs)
            cells.append(f'{name}: {totals[name][period][0]} сд {totals[name][period][1]:+.1f}R')
        print(f'{period:5s} пар {len(pairs)}, заявок {len(orders)} (без ставки фандинга {no_fund}) | '
              + ' | '.join(cells), flush=True)
    print('\nИТОГ (итог R; R на сделку [95%]; по периодам: итог R / просадка счёта при риске 1%)')
    for key, per in totals.items():
        allr = np.concatenate([np.array(v[3]) for v in per.values()]) if per else np.array([])
        if not len(allr):
            continue
        lo, hi = ci(allr)
        print(f'  {key:52s} {len(allr):5d} сд {allr.sum():+8.1f}R {allr.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]  '
              + ' '.join(f'{p} {per[p][1]:+6.1f}/{per[p][2]:.0f}%' for p in per))


def run(orders, exec_data, base):
    from smc_engine import run_portfolio
    return run_portfolio(orders, exec_data, **base)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1:2]
    if cmd == ['gen']:
        generate(int(os.getenv('FO_PROCS', '3')))
    elif cmd == ['eval']:
        evaluate()
