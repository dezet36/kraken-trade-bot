"""
Фибо так, как её исполняет живой бот, — точная симуляция (26.09.2026).

ЗАЧЕМ. research/backtest_smc.fibo_orders ставил заявку на ОТКРЫТИИ часовой
свечи сигнала, считая сигнал по свече целиком: 27–29% сделок наливались ценой,
которая сигнал и породила, и давали 64–70% прибыли бэктеста. С заявкой на
закрытии свечи фибо в минусе. Живой бот — между: pair_scanner берёт
fetch_ohlcv('1h') С ФОРМИРУЮЩЕЙСЯ свечой и считает сигнал раз в 5 минут.
Здесь это воспроизведено:

  • сигнал — по 47 закрытым часовым свечам + формирующейся (сложенной из
    закрытых 5-минуток часа) на каждом 5-минутном шаге; 4ч для фильтра тренда —
    тоже с формирующейся свечой;
  • заявка — в момент шага, на 0.1% хуже входа (strategy_profile.
    limit_offset_pct), с пределом издержек 5% от цены заявки
    (strategy_profile.cost_limit_pct('FIBO'), risk_gate.cost_too_high);
  • брокер — заявка занимает пару, пауза 12 ч и с постановки, снятие у цели
    (drops_at_target), срок 72 ч, безубыток на уровне B, удержание до 336 ч;
    слотов и направленного кэпа у фибо нет (сверено с сервером 26.09.2026).

Код стратегии — БОЕВОЙ (strategy.find_recent_impulse, analyze_market,
get_htf_trend). Подменена одна функция — strategy.find_local_extremes: она
перебирает строки таблицы по одной (df.iloc[i] в цикле) и съедала 33 мс из 70
на оценку. Здесь её копия на массивах numpy; перед прогоном копия сверяется с
оригиналом на тысячах окон и при первом расхождении прогон останавливается.

Запуск:  python research/fibo_live_sim.py backtest_cache_12m backtest_cache_bear
Заявки кладутся в results/fibo_live_orders_<кэш>.pkl, итог — в stdout.
"""
import os
import pickle
import sys
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
# Своя пустая папка данных: настройки оператора — по умолчанию, как на сервере
# (сверено 26.09.2026: минимальный стоп 0.8%, риск 0.5%, слотов без предела),
# а не локальный runtime_settings.json разработки.
os.environ.setdefault('BOT_DATA_DIR', os.path.join(HERE, 'results', 'sim-data'))
os.makedirs(os.environ['BOT_DATA_DIR'], exist_ok=True)

import logger  # noqa: E402
logger.log = lambda *a, **k: None          # строка на каждую оценку тормозила прогон

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import backtest_smc as bt  # noqa: E402
import config  # noqa: E402
import risk_gate  # noqa: E402
import strategy  # noqa: E402
import strategy_profile  # noqa: E402
from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402

STEP = np.timedelta64(5, 'm')
HOUR = np.timedelta64(1, 'h')
H4 = np.timedelta64(4, 'h')
OFFSET = strategy_profile.limit_offset_pct('FIBO')
COST_LIMIT = strategy_profile.cost_limit_pct('FIBO')
RESULTS = os.path.join(HERE, 'results')
_original_extremes = strategy.find_local_extremes


def fast_local_extremes(df, n=2):
    """strategy.find_local_extremes на массивах: тот же порядок, те же поля."""
    h = df['high'].to_numpy(dtype=float)
    lo = df['low'].to_numpy(dtype=float)
    ts = df['timestamp']
    highs, lows = [], []
    m = len(h)
    if m <= 2 * n:
        return highs, lows
    idx = np.arange(n, m - n)
    ch = np.ones(len(idx), dtype=bool)
    cl = np.ones(len(idx), dtype=bool)
    for j in range(1, n + 1):
        ch &= (h[idx] > h[idx - j]) & (h[idx] > h[idx + j])
        cl &= (lo[idx] < lo[idx - j]) & (lo[idx] < lo[idx + j])
    for i in idx[ch]:
        highs.append({'index': int(i), 'price': h[i], 'time': ts.iloc[i]})
    for i in idx[cl]:
        lows.append({'index': int(i), 'price': lo[i], 'time': ts.iloc[i]})
    return highs, lows


def check_equivalence(df, samples=3000, seed=1):
    """Копия обязана совпасть с оригиналом на каждом окне."""
    rng = np.random.default_rng(seed)
    lb = config.LOOKBACK_CANDLES
    for start in rng.integers(0, len(df) - lb - 1, size=samples):
        seg = df.iloc[start:start + lb].reset_index(drop=True)
        a = _original_extremes(seg, n=2)
        b = fast_local_extremes(seg, n=2)
        for x, y in zip(a, b):
            if len(x) != len(y) or any(p['index'] != q['index'] or float(p['price']) != float(q['price'])
                                       or p['time'] != q['time'] for p, q in zip(x, y)):
                raise SystemExit(f'КОПИЯ РАСХОДИТСЯ С ОРИГИНАЛОМ на окне с {start}: {x} против {y}')


def naive(series):
    return pd.DatetimeIndex(pd.to_datetime(series, utc=True)).tz_convert('UTC').tz_localize(None).to_numpy()


def orders_for_pair(args):
    """Все заявки пары: сигнал на каждом 5-минутном шаге по формирующейся свече."""
    cache, pair = args
    bt.CACHE_DIR = os.path.join(HERE, cache)
    strategy.find_local_extremes = fast_local_extremes
    data = bt.load_pair(pair)
    h1, m5 = data['1h'], data['5m']
    t1 = naive(h1['timestamp'])
    t5 = naive(m5['timestamp'])
    o5, hi5, lo5, c5, v5 = (m5[c].to_numpy(dtype=float) for c in ('open', 'high', 'low', 'close', 'volume'))
    cols = ['open', 'high', 'low', 'close', 'volume']
    arr1 = h1[cols].to_numpy(dtype=float)
    ts1 = h1['timestamp']
    h4 = data['4h']
    t4 = naive(h4['timestamp'])
    arr4 = h4[cols].to_numpy(dtype=float)
    ts4 = h4['timestamp']
    lb = config.LOOKBACK_CANDLES
    expiry = np.timedelta64(int(strategy_profile.expiry_hours('FIBO') * 3600), 's')
    blocked = set(getattr(config, 'BLOCK_ENTRY_HOURS_UTC', ()) or ())

    orders, seen = [], set()
    for i in range(lb + 10, len(h1)):
        hour = t1[i]
        a = int(np.searchsorted(t5, hour, side='left'))
        b = int(np.searchsorted(t5, hour + HOUR, side='left'))
        closed = h1.iloc[i - lb + 1:i]                         # 47 закрытых часов
        # 4ч: закрытые свечи до начала текущей четырёхчасовки + формирующаяся.
        k4 = int(np.searchsorted(t4, hour, side='right')) - 1
        start4 = t4[k4] if k4 >= 0 else hour
        a4 = int(np.searchsorted(t5, start4, side='left'))
        for k in range(1, b - a + 1):                          # k закрытых 5-минуток часа
            now = hour + k * STEP
            if int(pd.Timestamp(now).hour) in blocked:
                continue
            j = a + k
            part = [o5[a], hi5[a:j].max(), lo5[a:j].min(), c5[j - 1], v5[a:j].sum()]
            window = pd.concat([closed, pd.DataFrame([[ts1.iloc[i], *part]], columns=['timestamp', *cols])],
                               ignore_index=True)
            setup = strategy.find_recent_impulse(window, lookback_candles=lb)
            if not setup:
                continue
            if setup['size'] / setup['end_price'] * 100 < config.MIN_IMPULSE_PCT:
                continue
            part4 = [o5[a4], hi5[a4:j].max(), lo5[a4:j].min(), c5[j - 1], v5[a4:j].sum()]
            closed4 = h4.iloc[max(0, k4 - 219):k4]
            win4 = pd.concat([closed4, pd.DataFrame([[ts4.iloc[k4] if k4 >= 0 else ts1.iloc[i], *part4]],
                                                    columns=['timestamp', *cols])], ignore_index=True)
            trend = strategy.get_htf_trend(win4)
            if (trend == 'BULLISH' and setup['type'] == 'SHORT') or (trend == 'BEARISH' and setup['type'] == 'LONG'):
                continue
            signal = strategy.analyze_market(window, None, pair, 10_000)
            if not signal:
                continue
            key = (pair, signal['setup']['type'], round(signal['setup']['start_price'], 8),
                   round(signal['setup']['end_price'], 8))
            if key in seen:
                continue
            seen.add(key)
            prm = signal['params']
            long_ = signal['setup']['type'] == 'LONG'
            limit = prm['entry'] * (1 + OFFSET) if long_ else prm['entry'] * (1 - OFFSET)
            share = risk_gate.entry_cost_share(limit, abs(limit - prm['stop_loss']),
                                               config.ENTRY_COST_ROUND_TRIP) * 100
            orders.append(Order(
                pair=pair, direction='LONG' if long_ else 'SHORT', entry=limit, stop=prm['stop_loss'],
                targets=[prm['take_profit_1']], fractions=[1.0], created=now, expires=now + expiry,
                key=key, be_trigger=prm['be_level'] if config.BREAKEVEN_AT_B else None,
                meta={'rr': prm['rr'], 'cost_share': share, 'minute': k * 5},
            ))
    return cache, pair, orders


def main(caches):
    os.makedirs(RESULTS, exist_ok=True)
    bt.CACHE_DIR = os.path.join(HERE, caches[0])
    probe = bt.load_pair('ETHUSDT')['1h']
    check_equivalence(probe)
    print('копия find_local_extremes совпала с оригиналом на 3000 окнах', flush=True)
    tasks = [(c, p) for c in caches for p in bt.DEFAULT_PAIRS]
    by_cache = {c: [] for c in caches}
    with Pool(processes=max(1, (os.cpu_count() or 2))) as pool:
        for cache, pair, found in pool.imap_unordered(orders_for_pair, tasks):
            by_cache[cache] += found
            print(f'   {cache} {pair}: {len(found)} заявок', flush=True)
    for cache, orders in by_cache.items():
        with open(os.path.join(RESULTS, f'fibo_live_orders_{cache}.pkl'), 'wb') as fh:
            pickle.dump(orders, fh)
        report(cache, orders)


def report(cache, orders):
    bt.CACHE_DIR = os.path.join(HERE, cache)
    exec_data = {p: d['5m'] for p in bt.DEFAULT_PAIRS if (d := bt.load_pair(p)) is not None}
    base = dict(risk_pct=bt.RISK_PCT, max_positions=99, cooldown_hours=strategy_profile.cooldown_hours('FIBO'),
                breakeven_after_tp1=False, max_hold_hours=strategy_profile.max_hold_hours('FIBO') or 336.0,
                max_same_direction=0, occupy_while_pending=True, cooldown_from_placement=True)
    within = [o for o in orders if o.meta['cost_share'] <= COST_LIMIT]
    variants = [
        ('ЖИВАЯ ФИБО: предел издержек 5%, снятие у цели', within, dict(cancel_at_target=True)),
        ('то же без снятия у цели', within, dict(cancel_at_target=False)),
        ('без предела издержек, снятие у цели', orders, dict(cancel_at_target=True)),
    ]
    print(f'\n== {cache}: заявок {len(orders)}, в пределе издержек {len(within)}')
    for name, pool_, extra in variants:
        out = run_portfolio(pool_, exec_data, **{**base, **extra})
        trades = out['trades']
        total = sum(t['pnl'] / t['risk'] for t in trades)
        s = compute_stats(out)
        print(f"   {name:48s} {len(trades):5d} сд  {total:+8.1f}R  {total / max(1, len(trades)):+.3f} R/сд  "
              f"PF {s.get('profit_factor', 0):.3f}  просадка {s.get('max_dd_pct', 0):5.1f}%", flush=True)


if __name__ == '__main__':
    main(sys.argv[1:] or ['backtest_cache_12m'])
