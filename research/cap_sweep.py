"""
Кэп одновременных позиций фибо на пуле из 21 пары.

ЗАЧЕМ. MAX_ACTIVE_PAIRS=5 подбирался 2026-07-05 на пуле из 16 пар. Пул
вырос до 21 (2026-08-05), а кэп заново не мерился — это записано в config
как долг. Сторонний разбор 2026-09-19 назвал его первым в списке.

КАК МЕРЯЕТСЯ. Поток сетапов фибо строится ОДИН раз на период (это дорогая
часть: боевая analyze_market на каждой часовой свече), потом прогоняется
через портфель с кэпом 3, 4, 5, 6, 8 и без предела. Два независимых
периода: бычий 2025-26 и медвежий 2022-23. Пары — те же 21, что в
TRADING_PAIRS_POOL, из них берутся те, что есть в кэше периода.

ЧТО СМОТРЕТЬ. Не только суммарный R: кэп меняет и просадку, и число
одновременных позиций, а в крипте десять сделок в одну сторону — одна
ставка. Принимается кэп, лучший по отношению дохода к просадке НА ОБОИХ
периодах; если такого нет — остаётся 5.

Запуск:
    python research/cap_sweep.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

from smc_market_regime import BEAR_CACHE, BULL_CACHE  # noqa: E402

POOL = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT',
        'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT', 'ZECUSDT', 'SUIUSDT',
        'ARBUSDT', 'DOTUSDT', 'XLMUSDT', 'SHIB1000USDT', 'NEARUSDT', 'UNIUSDT',
        'AAVEUSDT', 'COTIUSDT', 'BICOUSDT']

CAPS = (3, 4, 5, 6, 8, 0)      # 0 — без предела


def load(cache_dir, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data = {}
    for pair in POOL:
        loaded = bt.load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    print(f'   пар в кэше: {len(data)} из {len(POOL)}', flush=True)
    return data, bt


def build(data, bt):
    import config
    config.BREAKEVEN_AT_B = True
    orders = []
    for pair in data:
        orders += bt.fibo_orders(pair, data[pair])
    print(f'   заявок: {len(orders)}', flush=True)
    return orders


def run(data, orders, cap):
    import copy

    import config
    from smc_engine import run_portfolio

    orders = [copy.copy(o) for o in orders]
    result = run_portfolio(
        orders, {p: data[p]['5m'] for p in data},
        risk_pct=config.RISK_PER_TRADE,
        max_positions=cap if cap else 10_000,
        cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
        max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0),
        breakeven_after_tp1=False)
    trades = [t for t in result['trades'] if t.get('risk')]
    if not trades:
        return None
    r = np.array([t['pnl'] / t['risk'] for t in trades])
    # Просадка по кривой R: максимум падения от пика накопленного R.
    curve = np.cumsum(r)
    peak = np.maximum.accumulate(curve)
    dd = float(np.max(peak - curve)) if len(curve) else 0.0
    # Одновременные позиции — по времени входа/выхода.
    events = []
    for t in trades:
        events.append((pd.Timestamp(t['entry_time']), 1))
        events.append((pd.Timestamp(t.get('exit_time') or t['entry_time']), -1))
    events.sort()
    open_now, max_open = 0, 0
    for _, delta in events:
        open_now += delta
        max_open = max(max_open, open_now)
    return {'trades': len(r), 'sum_r': float(r.sum()), 'mean_r': float(r.mean()),
            'win': float((r > 0).mean() * 100), 'dd_r': dd,
            'ratio': float(r.sum() / dd) if dd > 0 else float('inf'),
            'max_open': max_open}


def main():
    for label, cache in (('бык 2025-26', BULL_CACHE), ('медведь 2022-23', BEAR_CACHE)):
        data, bt = load(cache, label)
        orders = build(data, bt)
        print()
        print('=' * 96)
        print(f'{label.upper()} — пар {len(data)}, заявок {len(orders)}')
        print('=' * 96)
        head = (f'{"кэп":>6}{"сделок":>9}{"WR":>7}{"R/сделку":>11}{"сумма R":>10}'
                f'{"просадка R":>12}{"доход/прос.":>13}{"макс. одновр.":>15}')
        print(head)
        print('-' * len(head))
        for cap in CAPS:
            s = run(data, orders, cap)
            if s is None:
                print(f'{cap or "∞":>6}   сделок нет')
                continue
            print(f'{cap or "∞":>6}{s["trades"]:>9}{s["win"]:>6.0f}%{s["mean_r"]:>11.3f}'
                  f'{s["sum_r"]:>10.1f}{s["dd_r"]:>12.1f}{s["ratio"]:>13.2f}'
                  f'{s["max_open"]:>15}')
        print()


if __name__ == '__main__':
    main()
