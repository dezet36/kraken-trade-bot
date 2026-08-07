"""
Фибоначчи: только шорты и геометрия сделки (стоп, цель, отношение к риску).

ДВА ВОПРОСА ЗА ОДИН ПРОГОН.

1. ТОЛЬКО ШОРТЫ. Аудит показал, что в лонг стратегия не зарабатывает вовсе:
   +0.005 R на бычьем периоде (988 сделок) и ровно 0.000 на медвежьем (1423).
   Ноль на ОБОИХ периодах, включая бычий, где лонгам полагалось бы работать.
   Весь результат делают шорты: +0.051 и +0.079. Разбиение готовых сделок это
   показало, но занятость слотов оно не меняет — нужен полный прогон.

2. ОТНОШЕНИЕ РИСКА К ПРИБЫЛИ. Сейчас сделки открываются с RR около 1.23, и
   получается это так: вход на границе 38.2%, стоп за уровнем 0.886, цель на
   25% дальше конца импульса. Риск 0.514 размера импульса, прибыль 0.632.
   Поднять RR можно двумя рычагами — придвинуть стоп или отодвинуть цель:

       стоп 0.886 цель 0.25 → RR 1.23   (как сейчас)
       стоп 0.886 цель 0.50 → RR 1.72
       стоп 0.886 цель 0.75 → RR 2.20
       стоп 0.786 цель 0.50 → RR 2.13
       стоп 0.786 цель 0.75 → RR 2.73

   Бесплатного тут нет: дальняя цель берётся реже, ближний стоп выбивает
   чаще. Что перевесит — вопрос к данным, а не к арифметике.

ПОЧЕМУ ЭТО СЧИТАЕТСЯ БЫСТРО. Дорогая часть у этой стратегии — поиск сетапов:
боевая analyze_market зовётся на каждой часовой свече каждой пары. Но сам
сетап (начало и конец импульса) от уровней стопа и цели НЕ зависит: меняется
только геометрия сделки, построенная поверх него. Поэтому поиск делается один
раз на период, а варианты строятся из готовых сетапов. Десять вариантов стоят
столько же, сколько один.

ПРИЁМКА. Прежняя и двусторонняя: вариант принимается, только если он лучше
базового НА ОБОИХ периодах. Улучшение на одном — это выбор лучшей половины
монетки, и за эту работу такое трижды оказывалось ложным.

Запуск:
    python research/fibo_geometry.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BULL_CACHE, BULL_PAIRS, BEAR_CACHE, BEAR_PAIRS  # noqa: E402
from common import ci, diff_ci, hush, unhush, relax  # noqa: E402

PAIRS_LIMIT = 8


def collect_setups(pair, data):
    """
    Сетапы стратегии: импульс, цена входа и время. Без стопа и цели — они
    зависят от варианта и считаются потом.
    """
    import config
    import strategy

    df_1h = data['1h']
    lookback = config.LOOKBACK_CANDLES
    expiry = np.timedelta64(int(config.PENDING_ORDER_MAX_HOURS * 3600), 's')

    out, seen = [], set()
    for i in range(lookback + 10, len(df_1h)):
        window = df_1h.iloc[i - lookback: i + 1]
        signal = strategy.analyze_market(window, None, pair, 10_000)
        if not signal:
            continue
        setup = signal['setup']
        key = (pair, setup['type'], round(setup['start_price'], 8),
               round(setup['end_price'], 8))
        if key in seen:
            continue
        seen.add(key)
        now = pd.Timestamp(window.iloc[-1]['timestamp'])
        created = now.tz_convert('UTC').tz_localize(None).to_datetime64()
        out.append({
            'pair': pair,
            'type': setup['type'],
            'entry': signal['params']['entry'],
            'end': setup['end_price'],
            'size': setup['size'],
            'created': created,
            'expires': created + expiry,
            'key': key,
        })
    return out


def build_orders(setups, sl_level, tp_level, sides=('LONG', 'SHORT'), min_rr=1.1):
    """
    Сделки нужной геометрии из готовых сетапов.

    Формулы повторяют боевые (strategy.calculate_trade_params) буква в букву,
    включая буфер за уровнем стопа и пол минимального стопа. Иначе замерялась
    бы не стратегия, а её пересказ.
    """
    import config
    from smc_engine import Order
    import settings_store as settings

    min_stop = settings.min_stop_pct('FIBO')
    orders = []
    for s in setups:
        if s['type'] not in sides:
            continue
        entry, end, size = s['entry'], s['end'], s['size']
        if s['type'] == 'LONG':
            stop = end - size * sl_level - size * config.SL_BUFFER
            floor_ = entry * min_stop
            if entry - stop < floor_:
                stop = entry - floor_
            target = end + size * tp_level
        else:
            stop = end + size * sl_level + size * config.SL_BUFFER
            floor_ = entry * min_stop
            if stop - entry < floor_:
                stop = entry + floor_
            target = end - size * tp_level

        distance = abs(entry - stop)
        if distance <= 0:
            continue
        rr = abs(target - entry) / distance
        if rr < min_rr:
            continue

        orders.append(Order(
            pair=s['pair'],
            direction='LONG' if s['type'] == 'LONG' else 'SHORT',
            entry=entry, stop=stop, targets=[target], fractions=[1.0],
            created=s['created'], expires=s['expires'], key=s['key'],
            be_trigger=end if config.BREAKEVEN_AT_B else None,
            meta={'rr': rr, 'direction': s['type']},
        ))
    return orders


def run_variant(setups, data, sl_level, tp_level, sides):
    import config
    from smc_engine import compute_stats, run_portfolio

    orders = build_orders(setups, sl_level, tp_level, sides)
    if not orders:
        return None
    result = run_portfolio(
        orders, {p: data[p]['5m'] for p in data},
        risk_pct=config.RISK_PER_TRADE,
        max_positions=getattr(config, 'MAX_OPEN_POSITIONS', 5),
        cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
        max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0),
        breakeven_after_tp1=False)
    trades = [t for t in result['trades'] if t.get('risk')]
    if not trades:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    planned = np.array([(t.get('meta') or {}).get('rr', 0) for t in trades], float)
    return {'r': r, 'n': len(trades), 'mean': r.mean(), 'total': r.sum(),
            'winrate': (r > 0).mean() * 100, 'rr': np.median(planned),
            'dd': stats['max_dd_pct'], 'ret': stats['return_pct']}


def load(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt
    import config

    print(f'[{label}] загрузка...', flush=True)
    data = {}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is not None:
            data[pair] = loaded

    saved = relax(config)
    quiet = hush()
    setups = []
    try:
        for pair in data:
            setups += collect_setups(pair, data[pair])
            print(f'      {pair}: сетапов всего {len(setups)}', flush=True)
    finally:
        unhush(quiet)
        for name, value in saved.items():
            setattr(config, name, value)
    return data, setups


VARIANTS = [
    ('как сейчас',            0.886, 0.25, ('LONG', 'SHORT')),
    ('только шорты',          0.886, 0.25, ('SHORT',)),
    ('цель 0.50',             0.886, 0.50, ('LONG', 'SHORT')),
    ('цель 0.75',             0.886, 0.75, ('LONG', 'SHORT')),
    ('цель 1.00',             0.886, 1.00, ('LONG', 'SHORT')),
    ('стоп 0.786 цель 0.50',  0.786, 0.50, ('LONG', 'SHORT')),
    ('стоп 0.786 цель 0.75',  0.786, 0.75, ('LONG', 'SHORT')),
    ('шорты + цель 0.50',     0.886, 0.50, ('SHORT',)),
    ('шорты + цель 0.75',     0.886, 0.75, ('SHORT',)),
    ('шорты + 0.786/0.50',    0.786, 0.50, ('SHORT',)),
    ('шорты + 0.786/0.75',    0.786, 0.75, ('SHORT',)),
]


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, setups) in periods.items():
        print()
        print('=' * 100)
        print(f'{label}   сетапов: {len(setups)}')
        print('=' * 100)
        head = (f'{"вариант":<24}{"сделок":>8}{"RR план":>9}{"винрейт":>9}'
                f'{"R/сделку":>10}{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"доход/DD":>10}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, sl, tp, sides in VARIANTS:
            res = run_variant(setups, data, sl, tp, sides)
            if res is None:
                print(f'{name:<24}{"нет сделок":>8}')
                continue
            results[label][name] = res
            ratio = res['ret'] / res['dd'] if res['dd'] else 0
            print(f'{name:<24}{res["n"]:>8}{res["rr"]:>9.2f}{res["winrate"]:>8.1f}%'
                  f'{res["mean"]:>10.3f}{res["total"]:>9.1f}{res["ret"]:>9.1f}'
                  f'{res["dd"]:>7.1f}{ratio:>10.2f}')

    print()
    print('=' * 100)
    print('СРАВНЕНИЕ С НЫНЕШНЕЙ НАСТРОЙКОЙ (интервал разницы средних)')
    print('=' * 100)
    head = f'{"вариант":<24}' + ''.join(f'{p:>36}' for p in results)
    print(head)
    print('-' * len(head))
    for name, _sl, _tp, _sides in VARIANTS[1:]:
        cells = ''
        verdicts = []
        for period, table in results.items():
            base = table.get('как сейчас')
            cur = table.get(name)
            if not base or not cur:
                cells += f'{"—":>36}'
                verdicts.append(False)
                continue
            gain = cur['mean'] - base['mean']
            lo, hi = diff_ci(cur['r'], base['r'])
            crosses = not (lo > 0 or hi < 0)
            cells += f'{f"{gain:+.3f} [{lo:+.3f}; {hi:+.3f}]":>36}'
            verdicts.append(gain > 0 and not crosses)
        mark = '  ЛУЧШЕ на обоих' if all(verdicts) else ''
        print(f'{name:<24}{cells}{mark}')

    print()
    print('Принимается только то, что лучше на ОБОИХ периодах и чей интервал')
    print('не накрывает ноль. Рост RR сам по себе не цель: дальняя цель берётся')
    print('реже, и выигрыш в отношении может съесться падением попаданий.')


if __name__ == '__main__':
    main()
