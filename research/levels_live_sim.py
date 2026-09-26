"""
Уровни: точная симуляция боевого сканера против бэктеста — на одних свечах.

ЗАЧЕМ. На бумаге с 18.09.2026 уровни дали 2 сделки за неделю, а бэктест
(levels_backtest.py) обещал около одной в день и +0.3…+0.4R на сделку. У
фибо такое расхождение оказалось заглядыванием вперёд, и её «преимущество»
исчезло (fibo_live_sim.py). Здесь — та же проверка для уровней.

ЧЕМ БЭКТЕСТ ОТЛИЧАЕТСЯ ОТ БОЯ. Заявка у обоих — на закрытии свечи возврата,
ядро одно (levels/core.evaluate). Отличаются УРОВНИ:
    бэктест — build_levels один раз по ВСЕЙ истории: касания, случившиеся
              позже, склеиваются с прошлыми; уровень доступен, когда известно
              его ПОСЛЕДНЕЕ касание, а цена его — среднее по всем, будущим тоже;
    бой     — каждый час build_levels по последним LOOKBACK+4 закрытым свечам
              (strategy_levels._context): только прошлое, как есть.
Здесь на каждой закрытой часовой свече уровни строятся ровно как в бою.

Обе стопки заявок идут через ОДИНАКОВЫЕ правила живого брокера: слоты без
предела, кулдаун с постановки, заявка занимает пару, снятие у цели, вход
стоп-заявкой по закрытию свечи возврата со смещением лимита.

Запуск: python research/levels_live_sim.py [fresh|12m|bear ...]
"""

import os
import pickle
import sys
import time
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, HERE)

from levels import core, params as LP  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402

RESULTS = os.path.join(HERE, 'results')
PERIODS = {
    'fresh': ('backtest_cache_fresh', None),
    '12m': ('backtest_cache_12m', 'BULL_PAIRS'),
    'bear': ('backtest_cache_bear', 'BEAR_PAIRS'),
}
LIVE_WINDOW = LP.LOOKBACK + 4        # бой берёт LOOKBACK+5 и отбрасывает идущую свечу
LIMIT_OFFSET = 0.001                 # смещение лимита уровней (strategy_profile)


def load(cache, pair):
    import backtest_smc as bt
    bt.CACHE_DIR = os.path.join(HERE, cache)
    return bt.load_cached(pair, '1h'), bt.load_cached(pair, '5m')


def _order(pair, setup, created, key, source):
    expiry = np.timedelta64(int(LP.EXPIRY_HOURS * 3600), 's')
    long_side = setup['direction'] == 'LONG'
    entry = setup['entry'] * (1 + LIMIT_OFFSET if long_side else 1 - LIMIT_OFFSET)
    return Order(pair=pair, direction=setup['direction'], entry=entry,
                 stop=setup['stop_loss'], targets=[setup['target']], fractions=[1.0],
                 created=created, expires=created + expiry, key=key, entry_type='stop',
                 meta={'source': source, 'rr': setup['rr'], 'level': setup['level']})


def orders_for(task):
    """(кэш, пара) -> (заявки бэктеста, заявки боя). Одна пара — один процесс."""
    cache, pair = task
    df, _ = load(cache, pair)
    if df is None or len(df) < LIVE_WINDOW + 10:
        return pair, [], []
    ts = df['timestamp'].dt.tz_convert('UTC').dt.tz_localize(None).to_numpy(dtype='datetime64[ns]')
    high, low, close = (df[c].to_numpy(dtype=float) for c in ('high', 'low', 'close'))
    volume = df['volume'].to_numpy(dtype=float)
    hour = np.timedelta64(3600, 's')

    # Бэктест как есть: уровни по всей истории, решение на каждой свече.
    full_levels = core.build_levels(high, low)
    full_atr = core.atr(high, low, close)
    bt_orders, seen = [], set()
    for i in range(LIVE_WINDOW, len(df)):
        setup, _ = core.evaluate(high, low, close, volume, i, levels=full_levels, atr_values=full_atr)
        if setup is None:
            continue
        key = (pair, round(setup['level'], 8), setup['direction'], setup['pierce_index'])
        if key in seen:
            continue
        seen.add(key)
        bt_orders.append(_order(pair, setup, ts[i] + hour, key, 'backtest'))

    # Бой: на каждой закрытой свече уровни по последним LIVE_WINDOW свечам.
    live_orders, seen = [], set()
    for i in range(LIVE_WINDOW, len(df)):
        lo = i - LIVE_WINDOW + 1
        h, l, c, v = high[lo:i + 1], low[lo:i + 1], close[lo:i + 1], volume[lo:i + 1]
        setup, _ = core.evaluate(h, l, c, v, len(c) - 1,
                                 levels=core.build_levels(h, l), atr_values=core.atr(h, l, c))
        if setup is None:
            continue
        key = (pair, round(setup['level'], 8), setup['direction'], lo + setup['pierce_index'])
        if key in seen:
            continue
        seen.add(key)
        live_orders.append(_order(pair, setup, ts[i] + hour, key, 'live'))
    return pair, bt_orders, live_orders


def pairs_of(cache, attr):
    if attr:
        import smc_market_regime as mr
        return list(getattr(mr, attr))
    names = sorted({f.rsplit('_', 1)[0] for f in os.listdir(os.path.join(HERE, cache)) if f.endswith('_1h.pkl')})
    return names


def build(period):
    cache, attr = PERIODS[period]
    path = os.path.join(RESULTS, f'levels_live_orders_{period}.pkl')
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            return pickle.load(fh)
    tasks = [(cache, p) for p in pairs_of(cache, attr)
             if os.path.exists(os.path.join(HERE, cache, f'{p}_1h.pkl'))]
    started = time.time()
    with Pool(4) as pool:
        got = pool.map(orders_for, tasks)
    out = {pair: (bt_o, live_o) for pair, bt_o, live_o in got}
    with open(path, 'wb') as fh:
        pickle.dump(out, fh)
    print(f'   {period}: заявки собраны за {time.time() - started:.0f} с', flush=True)
    return out


def portfolio(orders, exec_data):
    return run_portfolio(orders, exec_data, risk_pct=LP.RISK_PCT, max_positions=99,
                         cooldown_hours=LP.COOLDOWN_HOURS, breakeven_after_tp1=False,
                         max_hold_hours=LP.MAX_HOLD_HOURS, max_same_direction=LP.MAX_SAME_DIRECTION,
                         cancel_at_target=True, occupy_while_pending=True,
                         cooldown_from_placement=True)


def line(label, orders, exec_data):
    if not orders:
        return f'   {label:34s} заявок нет'
    res = portfolio(orders, exec_data)
    tr = res['trades']
    if not tr:
        return f'   {label:34s} заявок {len(orders):5d}, сделок нет'
    st = compute_stats(res)
    r = np.array([t['pnl'] / t['risk'] for t in tr if t.get('risk')])
    return (f'   {label:34s} заявок {len(orders):5d} · сделок {len(tr):5d} · WR {(r > 0).mean() * 100:4.1f}% · '
            f'{r.mean():+.3f}R/сд · сумма {r.sum():+7.1f}R · PF {st["profit_factor"]:.2f} · '
            f'просадка {st["max_dd_pct"]:4.1f}%')


def main(periods):
    sys.stdout.reconfigure(encoding='utf-8')
    for period in periods:
        cache, _attr = PERIODS[period]
        got = build(period)
        exec_data = {}
        for pair in got:
            _h, m5 = load(cache, pair)
            if m5 is not None:
                exec_data[pair] = m5
        bt_all = [o for pair in got for o in got[pair][0]]
        live_all = [o for pair in got for o in got[pair][1]]
        bt_keys = {(o.pair, o.direction, str(o.created)) for o in bt_all}
        live_keys = {(o.pair, o.direction, str(o.created)) for o in live_all}
        print(f'\n== {period} ({cache}, пар {len(got)})')
        print(f'   совпали по времени и стороне: {len(bt_keys & live_keys)} · только бэктест: '
              f'{len(bt_keys - live_keys)} · только бой: {len(live_keys - bt_keys)}')
        print(line('БЭКТЕСТ (уровни по всей истории)', bt_all, exec_data))
        print(line('БОЙ (уровни по окну, как в боте)', live_all, exec_data))
        for side in ('LONG', 'SHORT'):
            print(line(f'  бой, только {side}', [o for o in live_all if o.direction == side], exec_data))
        if period == 'fresh':
            since = np.datetime64('2026-09-18T21:00')
            wk_bt = [o for o in bt_all if o.created >= since]
            wk_live = [o for o in live_all if o.created >= since]
            print(f'   с 18.09 21:00 (неделя бумаги): бэктест {len(wk_bt)} заявок, бой {len(wk_live)}: '
                  + ', '.join(f'{o.pair[:-4]} {o.direction} {str(o.created)[5:16]}' for o in wk_live))
        sys.stdout.flush()


if __name__ == '__main__':
    main(sys.argv[1:] or ['fresh', '12m', 'bear'])
