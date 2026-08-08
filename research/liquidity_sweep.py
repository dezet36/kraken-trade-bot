"""
Вынос скопления стопов, цель ЗА противоположным забором. Четыре периода.

ЧТО ЭТО ЗА СЕТАП И ЧЕМ ОТЛИЧАЕТСЯ ОТ ЗАКРЫТОГО. Стопы стоят не на уровне, а за
ним, и особенно густо — за равными максимумами и минимумами, видимой всем
чертой. Цена заходит за неё, собирает стопы и разворачивается; вход на возврате.

Ложный пробой границы коридора мерился и отвергнут. Отличие здесь одно, но
существенное — ЦЕЛЬ. Там она стояла на противоположном крае коридора, здесь —
ЗА противоположным скоплением, то есть дальше. Логика: цена, собрав стопы с
одной стороны, идёт к следующему их скоплению, а оно лежит за чертой, а не на
ней. Именно короткая цель губила и сетку (RR 0.38 требовал 72% попаданий), и
торговлю в коридоре.

ЧЕГО ЗДЕСЬ НЕТ. Дельты продавца, которая была в исходном замысле. Её отсутствие
измерено, а не предположено: в свече шесть полей, поля объёма покупателя нет;
лента отдаёт 1000 сделок — девять минут — и запрос в прошлое биржа игнорирует;
заменитель из свечи даёт связь со следующим баром −0.008. Проверить дельту на
истории НЕЛЬЗЯ. Сбор запущен, и через два-три месяца её добавление станет
отдельным замером с честной приёмкой.

ФАНДИНГ ВХОДИТ ЧАСОМ, А НЕ УРОВНЕМ. История ставок достаёт лишь на 400 дней и
проверочные периоды не покрывает. Но источники утверждают, что выносы учащаются
около СБРОСА фандинга (00:00, 08:00, 16:00 UTC), а для этого нужны только часы —
утверждение проверяемо бесплатно, и оно проверяется отдельным вариантом.

ЧЕТЫРЕ ПЕРИОДА, А НЕ ДВА, И ЭТО ГЛАВНОЕ ОТЛИЧИЕ ОТ ВСЕХ ПРЕЖНИХ ЗАМЕРОВ.
Измерение показало, что оба прежних проверочных периода — ПАДАЮЩИЕ: у
«бычьего» BTC −39.8%, у «медвежьего» −34.7%. Двусторонняя приёмка давала
независимость по времени, но не по режиму, и всё принятое могло быть отобрано
под падение. Здесь впервые участвуют растущие периоды: BTC +106.0% и +49.6%.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    в плюсе на ВСЕХ ЧЕТЫРЁХ периодах, интервал не накрывает ноль хотя бы на
    двух И просадка не больше 25% на каждом.

Требование ко всем четырём строже прежнего вдвое — и именно поэтому оно
записано: сетап, работающий только в падении, для бумажной торговли бесполезен,
потому что впереди неизвестный режим.

Запуск:
    python research/liquidity_sweep.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from common import RISING_CACHES, RISING_PAIRS, ci  # noqa: E402

PAIRS_LIMIT = 12
BAR_MIN = 60

PERIODS = [
    ('2022-01 падение', BEAR_CACHE, BEAR_PAIRS),
    ('2023-07 РОСТ',    RISING_CACHES[0], RISING_PAIRS),
    ('2024-07 РОСТ',    RISING_CACHES[1], RISING_PAIRS),
    ('2025-05 падение', BULL_CACHE, BULL_PAIRS),
]

BASE = {
    'tolerance': 0.35, 'min_touches': 2, 'pierce': 0.20, 'reclaim': 4,
    'beyond': 0.30, 'stop_pad': 0.25, 'funding_only': False,
}

VARIANTS = [
    ('база: цель за забором +0.30 ATR', {}),
    ('цель НА скоплении (без запаса)',  {'beyond': 0.0}),
    ('цель за забором +0.60 ATR',       {'beyond': 0.60}),
    ('скопление из 3 касаний',          {'min_touches': 3}),
    ('прокол глубже (0.35 ATR)',        {'pierce': 0.35}),
    ('допуск скопления 0.20 ATR',       {'tolerance': 0.20}),
    ('только у сброса фандинга',        {'funding_only': True}),
]


def scan(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt
    from liquidity import core

    print(f'[{label}] загрузка и пивоты...', flush=True)
    data, marks = {}, []
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or '1h' not in loaded:
            continue
        df = loaded['1h']
        data[pair] = df
        high = df['high'].to_numpy(float)
        low = df['low'].to_numpy(float)
        close = df['close'].to_numpy(float)
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        marks.append({'pair': pair, 'high': high, 'low': low, 'close': close,
                      'atr': core.atr_series(high, low, close),
                      'pivots': core.pivots(high, low),
                      'stamps': stamps.to_numpy(),
                      'hours': stamps.dt.hour.to_numpy()})
    print(f'      пар {len(data)}', flush=True)
    return data, marks


def build_orders(marks, cfg):
    from liquidity import core, params
    from smc_engine import Order

    life = np.timedelta64(params.EXPIRY_BARS * BAR_MIN * 60, 's')
    orders, seen_sweeps = [], 0
    gap = params.RECLAIM_BARS + 2

    for mark in marks:
        last = -10 ** 9
        n = len(mark['close'])
        for i in range(60, n - 2):
            if i - last < gap:
                continue
            # Час сброса фандинга: 00, 08, 16 UTC. Проверка стоит ДО поиска,
            # потому что она дешёвая, а поиск скоплений — нет.
            if cfg['funding_only'] and mark['hours'][i] not in params.FUNDING_HOURS:
                continue
            setup = core.find_sweep(
                mark['high'], mark['low'], mark['close'], i, mark['pivots'],
                mark['atr'], pierce_atr=cfg['pierce'],
                reclaim_bars=cfg['reclaim'], tolerance=cfg['tolerance'],
                min_touches=cfg['min_touches'])
            if setup is None:
                continue
            seen_sweeps += 1
            trade = core.build_trade(setup, stop_pad_atr=cfg['stop_pad'],
                                     beyond_atr=cfg['beyond'])
            if trade is None:
                continue
            last = i
            created = mark['stamps'][i]
            orders.append(Order(
                pair=mark['pair'], direction=setup['direction'],
                entry=trade['entry'], stop=trade['stop'],
                targets=[trade['target']], fractions=[1.0],
                created=created, expires=created + life,
                key=(mark['pair'], i), entry_type='stop',
                meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                      'touches': setup['pool']['touches'],
                      'direction': setup['direction']}))
    return orders, seen_sweeps


def run(marks, data, cfg):
    from liquidity import params
    from smc_engine import compute_stats, run_portfolio

    orders, sweeps = build_orders(marks, cfg)
    if len(orders) < 25:
        return None
    result = run_portfolio(
        orders, data, risk_pct=params.RISK_PCT,
        max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 25:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    rr = np.array([(o.meta or {}).get('rr', 0) for o in orders], float)
    longs = sum(1 for o in orders if o.direction == 'LONG')
    return {'r': r, 'n': len(trades), 'orders': len(orders), 'sweeps': sweeps,
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'wr': float((r > 0).mean() * 100), 'total': float(r.sum()),
            'dd': stats['max_dd_pct'], 'rr': float(np.median(rr)),
            'longs': longs / len(orders) * 100}


def main():
    periods = {}
    for label, cache, pairs in PERIODS:
        data, marks = scan(cache, pairs, label)
        if data:
            periods[label] = (data, marks)

    results = {}
    for label, (data, marks) in periods.items():
        print()
        print('=' * 118)
        print(f'{label}   пар: {len(data)}')
        print('=' * 118)
        head = (f'{"вариант":<34}{"выносов":>9}{"заявок":>8}{"сделок":>8}'
                f'{"RR":>6}{"лонг":>6}{"винрейт":>9}{"R вал.":>9}'
                f'{"R/сделку":>10}{"сумма":>8}{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        for name, override in VARIANTS:
            cfg = dict(BASE, **override)
            res = run(marks, data, cfg)
            results[(name, label)] = res
            if res is None:
                print(f'{name:<34}{"— мало сделок":>16}')
                continue
            lo, hi = ci(res['r'])
            print(f'{name:<34}{res["sweeps"]:>9}{res["orders"]:>8}{res["n"]:>8}'
                  f'{res["rr"]:>6.1f}{res["longs"]:>5.0f}%{res["wr"]:>8.1f}%'
                  f'{res["gross"]:>9.3f}{res["mean"]:>10.3f}{res["total"]:>8.1f}'
                  f'{res["dd"]:>7.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    labels = [label for label, _c, _p in PERIODS if label in periods]
    print()
    print('=' * 118)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на ВСЕХ ЧЕТЫРЁХ периодах,')
    print('интервал не накрывает ноль хотя бы на двух И просадка не больше 25%')
    print('на каждом. Требование ко всем четырём строже прежнего вдвое: сетап,')
    print('работающий только в падении, для бумаги бесполезен — впереди')
    print('неизвестный режим.')
    print('=' * 118)
    head = f'{"вариант":<34}' + ''.join(f'{lab:>21}' for lab in labels)
    print(head)
    print('-' * len(head))
    for name, _ in VARIANTS:
        cells, positive, strong, deep = '', [], 0, False
        for label in labels:
            res = results.get((name, label))
            if res is None:
                cells += f'{"—":>21}'
                positive.append(False)
                continue
            lo, _hi = ci(res['r'])
            positive.append(res['mean'] > 0)
            strong += 1 if lo > 0 else 0
            deep = deep or res['dd'] > 25
            cell = f'{res["mean"]:+.3f} DD{res["dd"]:.0f}%'
            cells += f'{cell:>21}'
        ok = positive and all(positive) and strong >= 2 and not deep
        print(f'{name:<34}{cells}{"  ПРИНЯТ" if ok else ""}')


if __name__ == '__main__':
    main()
