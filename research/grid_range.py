"""
Сетка в коридоре: окупают ли собранные проходы один выход из диапазона.

ПОЧЕМУ ЭТО НЕ ПЯТАЯ ПОПЫТКА ОДНОГО И ТОГО ЖЕ. Четыре закрытые за день идеи
роднит одно: все платили тейкера с обеих сторон, и при стопе 0.5% это 0.42 R
с каждой сделки. Здесь оба конца лимитные по построению — заявки стоят внутри
коридора, цена приходит к ним сама. Круг 0.040% вместо 0.210%.

На реальном коридоре BTC это проверено: шаг сетки 0.456%, издержки съедают
9% шага. У пробоя на пятиминутках они съедали весь край и ещё сверху.

ЧТО РЕШАЕТ ВСЁ. Один вопрос: сколько собранных проходов окупают один выход
цены из коридора. Сетка собирает много мелких прибылей и изредка платит одну
крупную — это продажа волатильности, и вся её судьба в этом соотношении.

ПРОФИЛЬ СДЕЛКИ ИЗВЕСТЕН ЗАРАНЕЕ И ОН НЕПРИЯТНЫЙ. Отношение риска к прибыли у
уровня 0.38-0.60, то есть безубыток требует 62-72% попаданий. Это «подбирание
монет», и вчерашний замер возврата после импульса показал, чем такое кончается:
метрики улучшались ровно по мере ослабления стопа, просадка падала, край таял,
а «результат» рос. Мы улучшали цифры, убирая контроль риска.

ПОЭТОМУ ПРИЁМКА ЗАПИСАНА ДО ПРОГОНА И СМЯГЧЕНИЮ НЕ ПОДЛЕЖИТ:

    в плюсе на ОБОИХ периодах, интервал не накрывает ноль,
    И просадка не больше 25% на обоих.

Оба периода трендовые — рост 2025-26 и падение 2022-23. Для сетки это худшие
условия, и именно поэтому проверка честная: если она проходит на обоих, это
не «работало до первого тренда».

ПОРОГИ КОРИДОРА ВЗЯТЫ ИЗ ДАННЫХ. Окно 48 часов, 9532 наблюдения по BTC:
ширина в ATR — 25-й процентиль 5.9, медиана 7.3; пересечений середины — 25-й
процентиль 2, медиана 4, 75-й 6. Ширина до 6 ATR при шести пересечениях даёт
13% окон, что и подтвердилось на прогоне движка.

РИСК ДЕЛИТСЯ МЕЖДУ УРОВНЯМИ, а не назначается каждому. Стоп у них общий, в
плохом случае они проигрывают одновременно — считать их независимыми ставками
значило бы занизить риск в MAX_FILLED раз.

Запуск:
    python research/grid_range.py
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
BAR_MIN = 60          # рабочий таймфрейм сетки — часовой

# Настройки поиска коридора требуют отдельного прохода: они меняют сам набор
# коридоров, а не геометрию сетки внутри него.
#   имя окна: (window, max_width_atr, min_crosses)
BOXES = {
    'база':            (48, 6.0, 6),
    'шире':            (48, 8.0, 6),
    'мягче по ходу':   (48, 6.0, 4),
    'окно 24':         (24, 6.0, 6),
    'окно 96':         (96, 6.0, 6),
}

#   имя, ключ коридора, уровней, набирать не более, запас стопа в ATR
VARIANTS = [
    ('база: 6 уровней · стоп +0.5 ATR', 'база',          6, 4, 0.5),
    ('уровней 4',                       'база',          4, 4, 0.5),
    ('уровней 10',                      'база',         10, 4, 0.5),
    ('набирать не более 2',             'база',          6, 2, 0.5),
    ('запас стопа 0.25 ATR',            'база',          6, 4, 0.25),
    ('запас стопа 1.0 ATR',             'база',          6, 4, 1.0),
    ('коридор шире (8 ATR)',            'шире',          6, 4, 0.5),
    ('мягче по ходу (4 пересечения)',   'мягче по ходу', 6, 4, 0.5),
    ('окно 24 часа',                    'окно 24',       6, 4, 0.5),
    ('окно 96 часов',                   'окно 96',       6, 4, 0.5),
]


def collect(pair, df, window, max_width, min_crosses):
    """
    Коридоры одной пары. Соседние бары дают почти одинаковые коридоры, поэтому
    повторы отбрасываются по округлённым границам: иначе каждый бар внутри
    боковика порождал бы свою сетку, и замер считал бы одно и то же по сто раз.
    """
    from grid import core

    high = df['high'].to_numpy(float)
    low = df['low'].to_numpy(float)
    close = df['close'].to_numpy(float)
    stamps = pd.to_datetime(df['timestamp'])
    if getattr(stamps.dt, 'tz', None) is not None:
        stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
    stamps = stamps.to_numpy()

    out, seen = [], set()
    for i in range(window + 20, len(close)):
        box = core.find_range(high, low, close, i, window, max_width, min_crosses)
        if not box:
            continue
        key = (pair, round(box['low'], 4), round(box['high'], 4))
        if key in seen:
            continue
        seen.add(key)
        box['pair'] = pair
        box['at'] = i
        box['time'] = stamps[i]
        out.append(box)
    return out


def build_orders(boxes, levels, max_filled, stop_pad):
    from grid import core, params
    from smc_engine import Order

    life = np.timedelta64(params.EXPIRY_BARS * BAR_MIN * 60, 's')
    orders = []
    for box in boxes:
        for spec in core.build_levels(box, levels, max_filled, stop_pad):
            created = box['time']
            orders.append(Order(
                pair=box['pair'], direction=spec['direction'],
                entry=spec['entry'], stop=spec['stop'],
                targets=[spec['target']], fractions=[1.0],
                created=created, expires=created + life,
                key=(box['pair'], int(box['at']), spec['level']),
                entry_type='limit',
                meta={'rr': spec['rr'], 'stop_pct': spec['stop_pct'],
                      'direction': spec['direction'],
                      'step_pct': spec['step'] / box['mid'] * 100},
            ))
    return orders


def run(boxes, data, levels, max_filled, stop_pad):
    from grid import params
    from smc_engine import compute_stats, run_portfolio

    orders = build_orders(boxes, levels, max_filled, stop_pad)
    if len(orders) < 10:
        return None
    result = run_portfolio(
        orders, data,
        # Риск делится между уровнями: сетка это ОДНА ставка с общим стопом.
        risk_pct=params.RISK_PCT / max(max_filled, 1),
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
    step = np.array([(o.meta or {}).get('step_pct', 0) for o in orders], float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'fill': len(trades) / len(orders) * 100,
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'step': float(np.median(step)) if len(step) else 0.0}


def load(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data, boxes = {}, {name: [] for name in BOXES}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or '1h' not in loaded:
            continue
        df = loaded['1h']
        data[pair] = df
        for name, (window, width, crosses) in BOXES.items():
            boxes[name] += collect(pair, df, window, width, crosses)
        print(f'      {pair}: коридоров базовых {len(boxes["база"])}', flush=True)
    return data, boxes


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, boxes) in periods.items():
        print()
        print('=' * 116)
        print(f'{label}   коридоров базовых: {len(boxes["база"])}   пар: {len(data)}')
        print('=' * 116)
        head = (f'{"вариант":<36}{"заявок":>8}{"сделок":>8}{"набрал":>8}'
                f'{"шаг %":>8}{"винрейт":>9}{"R вал.":>9}{"издержки":>10}'
                f'{"R/сделку":>10}{"сумма R":>9}{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, box_key, levels, filled, pad in VARIANTS:
            res = run(boxes[box_key], data, levels, filled, pad)
            if res is None:
                print(f'{name:<36}{"— мало сделок":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<36}{res["orders"]:>8}{res["n"]:>8}{res["fill"]:>7.0f}%'
                  f'{res["step"]:>8.3f}{res["wr"]:>8.1f}%{res["gross"]:>9.3f}'
                  f'{res["costs"]:>10.3f}{res["mean"]:>10.3f}{res["total"]:>9.1f}'
                  f'{res["dd"]:>7.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 116)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на ОБОИХ периодах,')
    print('интервал не накрывает ноль И просадка не больше 25% на обоих.')
    print('Второе условие смягчению не подлежит: при отношении риска к прибыли')
    print('0.38-0.60 безубыток требует 62-72% попаданий, и на таком запасе')
    print('просадка в половину депозита означает, что стратегия живёт до первой')
    print('кластеризации убытков, а не до конца периода.')
    print('=' * 116)
    for name, *_rest in VARIANTS:
        cells, ok = '', []
        for label, table in results.items():
            res = table.get(name)
            if not res:
                cells += f'{"—":>34}'
                ok.append(False)
                continue
            lo, hi = ci(res['r'])
            ok.append(res['mean'] > 0 and lo > 0 and res['dd'] <= 25)
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}] DD {res["dd"]:.0f}%'
            cells += f'{cell:>34}'
        mark = '  ПРИНЯТ' if all(ok) and ok else ''
        print(f'{name:<36}{cells}{mark}')

    print()
    print('Если валовый край отрицателен — сетка не работает как идея, и')
    print('издержки тут ни при чём. Если положителен, а чистый нет — дело в')
    print('издержках, и тогда имеет смысл разговор о шаге и комиссиях.')


if __name__ == '__main__':
    main()
