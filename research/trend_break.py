"""
Пробой канала на ДЛИННОМ горизонте. Четыре периода, третья попытка жанра.

ПОЧЕМУ ТРЕТЬЯ И ЧЕМ ОТЛИЧАЕТСЯ. Пробой отвергался дважды, и оба раза на
скальперском горизонте: сигнал на пятиминутках, уровни на часе, удержание часы.
Голый пробой канала дал −0.455 R [−0.64; −0.26], пробой после прижатия с
подтверждением закрытием и объёмом — тоже минус.

Отличие здесь не в надежде, а в арифметике издержек.

    горизонт     типичный стоп   круг тейкера   издержки в R
    скальпинг        0.5%           0.21%          0.42
    длинный          2-3%           0.21%          0.07-0.10

0.42 R с каждой сделки не переживёт никакой край — скальперская версия была
обречена ещё до вопроса о качестве сигнала. На длинном горизонте тот же круг
стоит вшестеро дешевле. Ровно это рассуждение сработало у Боллинджера: переход
с пятиминуток на час вдвое срезал издержки и перевёл знак через ноль, причём
механизм был назван ДО прогона и подтвердился.

Второе отличие — вопрос другой. За два дня трижды измерено, что на КОРОТКОМ
горизонте уход за уровень возвращается: вынос ликвидности, ложный пробой
коридора, отбой. Продолжается ли уход на ДЛИННОМ, не проверялось ни разу.

Третье — у следования за трендом есть доказательная база, в отличие от волн
Эллиотта: временной моментум подтверждён рецензируемыми работами и многолетним
живым результатом управляющих фондов.

ГЛАВНЫЙ РИСК — ЧИСЛО СДЕЛОК, И ОН НАЗВАН ДО ПРОГОНА. Канал в неделю при
удержании в дни даст 30-80 сделок на период. Если выйдет меньше тридцати,
замер не состоялся, и это будет сказано прямо, а не прикрыто широким интервалом.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    в плюсе на ВСЕХ ЧЕТЫРЁХ периодах, интервал не накрывает ноль хотя бы на
    двух, просадка не больше 30% на каждом И не меньше 30 сделок на период.

Просадка допущена выше обычных 25%: у этого жанра доля попаданий около 35-40%
при редких крупных выигрышах, и просадка структурно глубже. Но 30 сделок —
условие жёсткое: без него любой результат здесь был бы гаданием.

Запуск:
    python research/trend_break.py
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

PAIRS_LIMIT = 14
BAR_MIN = 60
MIN_TRADES = 30

PERIODS = [
    ('2022-01 падение', BEAR_CACHE, BEAR_PAIRS),
    ('2023-07 РОСТ',    RISING_CACHES[0], RISING_PAIRS),
    ('2024-07 РОСТ',    RISING_CACHES[1], RISING_PAIRS),
    ('2025-05 падение', BULL_CACHE, BULL_PAIRS),
]

# Длины каналов, которые понадобятся. Считаются один раз на период.
LENGTHS = (48, 96, 168, 336, 72, 24)

BASE = {'channel': 168, 'exit_channel': 72, 'stop': 3.0, 'break': 0.10,
        'exit_mode': 'channel', 'target_r': 3.0}

VARIANTS = [
    ('канал неделя (168ч), выход по каналу', {}),
    ('канал 2 суток (48ч)',                  {'channel': 48,
                                              'exit_channel': 24}),
    ('канал 4 суток (96ч)',                  {'channel': 96,
                                              'exit_channel': 48}),
    ('канал 2 недели (336ч)',                {'channel': 336,
                                              'exit_channel': 168}),
    ('стоп 2 ATR',                           {'stop': 2.0}),
    ('стоп 5 ATR',                           {'stop': 5.0}),
    ('цель 3R вместо канала',                {'exit_mode': 'target'}),
    ('цель 5R вместо канала',                {'exit_mode': 'target',
                                              'target_r': 5.0}),
    ('пробой глубже (0.30 ATR)',             {'break': 0.30}),
]


def scan(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt
    from trend import core

    print(f'[{label}] загрузка и каналы...', flush=True)
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
        bands = {length: core.channels(high, low, length) for length in LENGTHS}
        marks.append({'pair': pair, 'close': close, 'bands': bands,
                      'atr': core.atr_series(high, low, close),
                      'stamps': stamps.to_numpy()})
    print(f'      пар {len(data)}', flush=True)
    return data, marks


def build_orders(marks, cfg):
    from smc_engine import Order
    from trend import core, params

    life = np.timedelta64(params.EXPIRY_BARS * BAR_MIN * 60, 's')
    orders, breaks = [], 0
    for mark in marks:
        top, bottom = mark['bands'][cfg['channel']]
        ex_top, ex_bottom = mark['bands'][cfg['exit_channel']]
        close, atr, stamps = mark['close'], mark['atr'], mark['stamps']
        last = -10 ** 9
        for i in range(cfg['channel'] + 20, len(close) - 2):
            # Пауза равна каналу выхода: без неё один затяжной тренд порождал
            # бы сигнал на каждом баре, и замер посчитал бы одно движение сто
            # раз, раздув и число сделок, и уверенность в результате.
            if i - last < cfg['exit_channel']:
                continue
            setup = core.find_break(close, i, top, bottom, atr,
                                    break_atr=cfg['break'])
            if setup is None:
                continue
            breaks += 1
            trade = core.build_trade(
                setup, ex_top[i], ex_bottom[i], stop_atr=cfg['stop'],
                exit_mode=cfg['exit_mode'], target_r=cfg['target_r'])
            if trade is None:
                continue
            last = i
            created = stamps[i]
            orders.append(Order(
                pair=mark['pair'], direction=setup['direction'],
                entry=trade['entry'], stop=trade['stop'],
                targets=[trade['target']], fractions=[1.0],
                created=created, expires=created + life,
                key=(mark['pair'], i), entry_type='stop',
                meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                      'direction': setup['direction']}))
    return orders, breaks


def run(marks, data, cfg):
    from smc_engine import compute_stats, run_portfolio
    from trend import params

    orders, breaks = build_orders(marks, cfg)
    if len(orders) < 10:
        return None
    result = run_portfolio(
        orders, data, risk_pct=params.RISK_PCT,
        max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 10:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    rr = np.array([(o.meta or {}).get('rr', 0) for o in orders], float)
    stop_pct = np.array([(o.meta or {}).get('stop_pct', 0) for o in orders], float)
    longs = sum(1 for o in orders if o.direction == 'LONG')
    return {'r': r, 'n': len(trades), 'orders': len(orders), 'breaks': breaks,
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'rr': float(np.median(rr)), 'stop': float(np.median(stop_pct)),
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
        print('=' * 122)
        print(f'{label}   пар: {len(data)}')
        print('=' * 122)
        head = (f'{"вариант":<38}{"пробоев":>9}{"сделок":>8}{"стоп%":>7}'
                f'{"RR":>6}{"лонг":>6}{"винрейт":>9}{"R вал.":>9}{"издер.":>8}'
                f'{"R/сделку":>10}{"сумма":>8}{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        for name, override in VARIANTS:
            cfg = dict(BASE, **override)
            res = run(marks, data, cfg)
            results[(name, label)] = res
            if res is None:
                print(f'{name:<38}{"— мало сделок":>16}')
                continue
            lo, hi = ci(res['r'])
            print(f'{name:<38}{res["breaks"]:>9}{res["n"]:>8}{res["stop"]:>7.2f}'
                  f'{res["rr"]:>6.1f}{res["longs"]:>5.0f}%{res["wr"]:>8.1f}%'
                  f'{res["gross"]:>9.3f}{res["costs"]:>8.3f}{res["mean"]:>10.3f}'
                  f'{res["total"]:>8.1f}{res["dd"]:>7.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    labels = [label for label, _c, _p in PERIODS if label in periods]
    print()
    print('=' * 122)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на ВСЕХ ЧЕТЫРЁХ, интервал')
    print('не накрывает ноль хотя бы на двух, просадка не больше 30% на каждом')
    print(f'И не меньше {MIN_TRADES} сделок на период.')
    print('=' * 122)
    head = f'{"вариант":<38}' + ''.join(f'{lab:>20}' for lab in labels)
    print(head)
    print('-' * len(head))
    for name, _ in VARIANTS:
        cells, ok, strong, thin = '', [], 0, False
        for label in labels:
            res = results.get((name, label))
            if res is None:
                cells += f'{"—":>20}'
                ok.append(False)
                continue
            lo, _hi = ci(res['r'])
            ok.append(res['mean'] > 0 and res['dd'] <= 30)
            strong += 1 if lo > 0 else 0
            thin = thin or res['n'] < MIN_TRADES
            cell = f'{res["mean"]:+.3f} n={res["n"]}'
            cells += f'{cell:>20}'
        verdict = ''
        if thin:
            verdict = '  МАЛО СДЕЛОК — замер не состоялся'
        elif ok and all(ok) and strong >= 2:
            verdict = '  ПРИНЯТ'
        print(f'{name:<38}{cells}{verdict}')

    print()
    print('КАК ЧИТАТЬ. «R вал.» — край до издержек. Скальперская версия умирала')
    print('именно на них: 0.42 R с каждой сделки при стопе 0.5%. Если здесь')
    print('валовый край положителен, а чистый нет, разговор о горизонте имеет')
    print('смысл. Если отрицателен и валовый — жанр закрыт на трёх разных его')
    print('формулировках, и возвращаться к нему больше незачем.')


if __name__ == '__main__':
    main()
