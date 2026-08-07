"""
Ложный пробой границы коридора: spring и upthrust.

ОТКУДА ФОРМУЛИРОВКА. Из трёх собственных замеров, которые все три говорят
одно, и из внешних источников, которые объясняют почему:

    что торговали                       результат
    пробой уровня (вход на прорыве)     края нет, три оси проверены
    сетка (лимит НА краю коридора)      края нет ещё до издержек
    прокол с возвратом (вход ПОСЛЕ)     работает: 0.279 и 0.374 R

Механика из источников: за границами коридора скапливаются стопы, цена
выносит их ради объёма и разворачивается. Лимит на краю систематически
покупает у того, кто сметает стопы; вход на пробое оказывается вынесенной
стороной. Отсюда единственное оставшееся место — ПОСЛЕ возврата.

ЧТО ЗДЕСЬ НОВОГО ПРОТИВ НАШЕЙ СТРАТЕГИИ УРОВНЕЙ. Там любой уровень из пивотов
и цель на следующем уровне: отношение риска к прибыли около 2. Здесь нужен
подтверждённый КОРИДОР, а цель ставится на противоположном крае — в разы
дальше. Короткая цель и убила сетку: при RR 0.38 безубыток требовал 72%
попаданий.

ПРОТИВОРЕЧИЕ ПРО ОБЪЁМ ПРОВЕРЯЕТСЯ, А НЕ РАЗРЕШАЕТСЯ РАССУЖДЕНИЕМ. Вайкофф:
spring подтверждается НИЗКИМ объёмом на проколе — продавца нет, вынос
технический. Наша стратегия уровней: нужен ВЫСОКИЙ объём на возврате — за
отбоем кто-то стоит. Это утверждения о разных барах, и оба здесь варианты.

ПРИЁМКА ЗАПИСАНА ДО ПРОГОНА И СМЯГЧЕНИЮ НЕ ПОДЛЕЖИТ:

    в плюсе на ОБОИХ периодах, интервал не накрывает ноль,
    И просадка не больше 25% на обоих.

Оба периода трендовые. Для торговли внутри коридора это худшие условия, и
потому проверка честная.

Запуск:
    python research/range_sweep.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

PAIRS_LIMIT = 10
BAR_MIN = 60

#   имя, глубина прокола ATR, окно возврата, доля цели, объём прокола не выше,
#   объём возврата не ниже, тип входа
VARIANTS = [
    ('база: прокол 0.25 · цель весь коридор', 0.25, 4, 1.0, 0.0, 0.0, 'stop'),
    ('цель до середины',                      0.25, 4, 0.5, 0.0, 0.0, 'stop'),
    ('прокол глубже (0.5 ATR)',               0.50, 4, 1.0, 0.0, 0.0, 'stop'),
    ('возврат быстрее (2 бара)',              0.25, 2, 1.0, 0.0, 0.0, 'stop'),
    ('возврат дольше (6 баров)',              0.25, 6, 1.0, 0.0, 0.0, 'stop'),
    ('Вайкофф: объём прокола < 0.8 среднего', 0.25, 4, 1.0, 0.8, 0.0, 'stop'),
    ('наш: объём возврата > 1.2 среднего',    0.25, 4, 1.0, 0.0, 1.2, 'stop'),
    ('оба условия по объёму',                 0.25, 4, 1.0, 0.8, 1.2, 'stop'),
    ('вход лимитом (мейкер)',                 0.25, 4, 1.0, 0.0, 0.0, 'limit'),
]


def collect(pair, df, pierce, reclaim):
    """Сетапы одной пары при заданной глубине прокола и окне возврата."""
    from grid import core, sweep

    high = df['high'].to_numpy(float)
    low = df['low'].to_numpy(float)
    close = df['close'].to_numpy(float)
    volume = (df['volume'].to_numpy(float) if 'volume' in df.columns
              else np.ones(len(close)))
    stamps = pd.to_datetime(df['timestamp'])
    if getattr(stamps.dt, 'tz', None) is not None:
        stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
    stamps = stamps.to_numpy()

    out, seen = [], set()
    for i in range(80, len(close)):
        # КОРИДОР СТРОИТСЯ ДО ОКНА ПРОКОЛА. Если взять его на баре возврата,
        # окно вберёт сам прокол, граница сместится вместе с ним, и условие
        # «цена вышла за границу» станет невыполнимым: на реальных данных это
        # дало ноль сетапов на десяти тысячах баров.
        box = core.find_range(high, low, close, i - reclaim - 1)
        if not box:
            continue
        setup = sweep.find_sweep(high, low, close, volume, i, box,
                                 pierce, reclaim)
        if not setup:
            continue
        # Соседние бары дают почти один и тот же вынос — считаем его один раз.
        key = (pair, setup['direction'], setup['pierce_at'])
        if key in seen:
            continue
        seen.add(key)
        setup['pair'] = pair
        setup['at'] = i
        setup['time'] = stamps[i]
        out.append(setup)
    return out


def build_orders(setups, target_frac, max_pierce_vol, min_reclaim_vol, kind):
    from grid import sweep, sweep_params as P
    from smc_engine import Order

    life = np.timedelta64(P.EXPIRY_BARS * BAR_MIN * 60, 's')
    orders = []
    for s in setups:
        if max_pierce_vol and s['pierce_volume'] > max_pierce_vol:
            continue
        if min_reclaim_vol and s['reclaim_volume'] < min_reclaim_vol:
            continue
        trade = sweep.build_trade(s, target_frac)
        if trade is None:
            continue
        created = s['time']
        orders.append(Order(
            pair=s['pair'], direction=s['direction'],
            entry=trade['entry'], stop=trade['stop'],
            targets=[trade['target']], fractions=[1.0],
            created=created, expires=created + life,
            key=(s['pair'], s['direction'], int(s['pierce_at'])),
            entry_type=kind,
            meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                  'direction': s['direction'],
                  'depth': s['pierce_depth_atr']},
        ))
    return orders


def run(setups, data, target_frac, max_pv, min_rv, kind):
    from grid import sweep_params as P
    from smc_engine import compute_stats, run_portfolio

    orders = build_orders(setups, target_frac, max_pv, min_rv, kind)
    if len(orders) < 10:
        return None
    result = run_portfolio(
        orders, data,
        risk_pct=P.RISK_PCT, max_positions=P.MAX_POSITIONS,
        cooldown_hours=P.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=P.MAX_HOLD_BARS * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 10:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    rr = np.array([(o.meta or {}).get('rr', 0) for o in orders], float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'fill': len(trades) / len(orders) * 100,
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'rr': float(np.median(rr)) if len(rr) else 0.0}


def load(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    combos = sorted({(v[1], v[2]) for v in VARIANTS})
    data, setups = {}, {c: [] for c in combos}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or '1h' not in loaded:
            continue
        df = loaded['1h']
        data[pair] = df
        for combo in combos:
            setups[combo] += collect(pair, df, combo[0], combo[1])
        print(f'      {pair}: выносов базовых {len(setups[(0.25, 4)])}', flush=True)
    return data, setups


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, setups) in periods.items():
        print()
        print('=' * 118)
        print(f'{label}   выносов базовых: {len(setups[(0.25, 4)])}   пар: {len(data)}')
        print('=' * 118)
        head = (f'{"вариант":<40}{"заявок":>8}{"сделок":>8}{"набрал":>8}'
                f'{"RR":>7}{"винрейт":>9}{"R вал.":>9}{"издержки":>10}'
                f'{"R/сделку":>10}{"сумма R":>9}{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, pierce, reclaim, frac, max_pv, min_rv, kind in VARIANTS:
            res = run(setups[(pierce, reclaim)], data, frac, max_pv, min_rv, kind)
            if res is None:
                print(f'{name:<40}{"— мало сделок":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<40}{res["orders"]:>8}{res["n"]:>8}{res["fill"]:>7.0f}%'
                  f'{res["rr"]:>7.2f}{res["wr"]:>8.1f}%{res["gross"]:>9.3f}'
                  f'{res["costs"]:>10.3f}{res["mean"]:>10.3f}{res["total"]:>9.1f}'
                  f'{res["dd"]:>7.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 118)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на ОБОИХ периодах,')
    print('интервал не накрывает ноль И просадка не больше 25% на обоих.')
    print('=' * 118)
    for name, *_rest in VARIANTS:
        cells, ok = '', []
        for label, table in results.items():
            res = table.get(name)
            if not res:
                cells += f'{"—":>36}'
                ok.append(False)
                continue
            lo, hi = ci(res['r'])
            ok.append(res['mean'] > 0 and lo > 0 and res['dd'] <= 25)
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}] DD {res["dd"]:.0f}% n={res["n"]}'
            cells += f'{cell:>36}'
        mark = '  ПРИНЯТ' if all(ok) and ok else ''
        print(f'{name:<40}{cells}{mark}')

    print()
    print('Отдельно смотреть на две строки про объём: они проверяют')
    print('ПРОТИВОПОЛОЖНЫЕ утверждения о разных барах. Если сработает вайкоффское')
    print('(тихий прокол) — это против нашей же стратегии уровней, и её условие')
    print('придётся перепроверить. Если наше — источники ошибаются на наших данных.')


if __name__ == '__main__':
    main()
