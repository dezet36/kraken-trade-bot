"""
Фибоначчи: насколько глубоко заходить в зону A.

ВОПРОС. Сейчас вход стоит на ближней границе зоны A — коррекция 38.2% от
конца импульса. Зона A простирается до 61.8%, и её середина — это классический
уровень 50%. Что даёт вход глубже?

АРИФМЕТИКА ИЗВЕСТНА ЗАРАНЕЕ, И ЭТО ВАЖНО. Стоп у стратегии привязан не ко
входу, а к уровню 0.886 за концом импульса плюс буфер, то есть стоит на
месте, куда бы мы ни вошли. Цель тоже неподвижна — 25% за концом импульса.
Значит глубина входа двигает и риск, и прибыль ОДНОВРЕМЕННО:

    RR = (0.25 + r) / (0.896 - r),   где r — глубина коррекции

    r = 0.382  →  RR 1.23   (как сейчас)
    r = 0.500  →  RR 1.89
    r = 0.618  →  RR 3.12

Считать это замером не нужно — это деление. Замер нужен ради второго
следствия, которое в другую сторону: чем глубже лимит, тем реже до него
доходит цена. Часть сетапов развернётся раньше и не наберёт нас вовсе, а
часть — доедет до нашего входа и продолжит вниз, к стопу, до которого теперь
ближе. Что перевесит, арифметика не скажет.

ПОЧЕМУ ХВАТАЕТ ОДНОГО ПРОГОНА НА ПЕРИОД. Глубина входа влияет на две вещи:
на цену лимита и на окно, в котором сигнал вообще выдаётся (боевой код
требует, чтобы цена была ещё НЕ ГЛУБЖЕ входа). Окно тем шире, чем глубже
вход, поэтому сканируем один раз с самым глубоким вариантом — 61.8% — и
записываем цену на момент сигнала. Окна остальных вариантов получаются из
неё отбором: они строго уже. Так все три варианта считаются на одном и том
же наборе импульсов, и разница между ними — это разница входа, а не разница
выборок.

Порог RR на время поиска снят и применяется потом, по варианту: иначе
нынешний MIN_RR отсёк бы сетапы, которые при глубоком входе проходят с
запасом, и глубокий вариант мерился бы на обрезках.

ПРИЁМКА. Двусторонняя: лучше нынешнего на ОБОИХ периодах и интервал разницы
средних не накрывает ноль.

РЕЗУЛЬТАТ, 2026-08-06. ПРИНЯТЫ ДВА ВАРИАНТА — и оба только вместе с шортами.

    вариант                     R/сделку (бык / медведь)   разница с нынешним
    вход 50.0% (обе стороны)         0.079 / 0.036       +0.044 / -0.001
    вход 61.8% (обе стороны)         0.040 / 0.042       +0.006 / +0.005
    шорты · вход 38.2%               0.094 / 0.092       +0.060 / +0.055
    шорты · вход 50.0%               0.168 / 0.172       +0.134 / +0.135  ПРИНЯТ
    шорты · вход 61.8%               0.259 / 0.254       +0.224 / +0.217  ПРИНЯТ

СДЕЛОК ПРИ ЭТОМ НЕ МЕНЬШЕ, А БОЛЬШЕ: 1601 и 2107 против 1679 и 2287 у
нынешней настройки. Причина не в наполняемости — она как раз падает (37% ->
29% -> 25%), — а в том, что окно выдачи сигнала при глубоком входе ШИРЕ:
лимит ставится и в тех случаях, когда цена уже ушла ниже 38.2%. Заявок
становится вдвое больше, и это перекрывает падение доли исполненных.

ГЛУБИНА РАБОТАЕТ ТОЛЬКО НА ШОРТАХ. Без разделения по сторонам вход 50%
даёт +0.044 и -0.001 — интервалы накрывают ноль. То есть выигрыш дают не
глубокий вход сам по себе и не шорты сами по себе, а их сочетание.

ЧЕГО СТОИТ 61.8%. Просадка растёт: 24.3% и 23.2% против 17.0 и 18.1 у
пятидесяти. Расстояние до стопа падает до 0.89%, вплотную к полу в 0.8%, и
издержки в единицах R вырастают почти вдвое — 0.24 R против 0.15. Винрейт
28-36%. Доходность в таблице (737% и 1612%) считается сложным процентом от
суммы R и отражает не будущее, а лишь то, что край положителен и сделок
много; ориентироваться надо на R и просадку.

Запуск:
    python research/fibo_entry.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from fibo_audit import ci, diff_ci, hush, unhush  # noqa: E402

PAIRS_LIMIT = 8

# Самый глубокий вход из проверяемых. Сканирование идёт с ним, потому что его
# окно самое широкое, а окна остальных из него получаются отбором.
DEEPEST = 0.618


def collect_setups(pair, data):
    """
    Импульсы и цена на момент сигнала. Вход не фиксируется: он считается
    потом, по варианту.
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
            'pair': pair, 'type': setup['type'],
            'end': float(setup['end_price']), 'size': float(setup['size']),
            # Цена, по которой боевой код решает, не поздно ли ставить лимит.
            'price': float(window.iloc[-1]['close']),
            'created': created, 'expires': created + expiry, 'key': key,
        })
    return out


def build_orders(setups, depth, sides, min_rr):
    """
    Заявки для входа на глубине `depth` коррекции.

    Формулы повторяют боевые (strategy.calculate_trade_params) буква в букву:
    стоп за 0.886 от конца импульса плюс буфер 1% размера, пол по расстоянию
    до стопа из настроек, цель 25% за концом импульса.
    """
    import config
    import settings_store as settings
    from smc_engine import Order

    min_stop = settings.min_stop_pct('FIBO')
    orders = []
    for s in setups:
        if s['type'] not in sides:
            continue
        end, size = s['end'], s['size']

        if s['type'] == 'LONG':
            entry = end - size * depth
            # Окно боевого кода: лимит ставится, только если цена ЕЩЁ не
            # ушла глубже входа и не выше конца импульса.
            if not (entry <= s['price'] <= end):
                continue
            stop = end - size * config.SL_LEVEL_R - size * config.SL_BUFFER
            # Пол по расстоянию до стопа — строчка в строчку как в бою.
            # Здесь он важнее обычного: чем глубже вход, тем ближе стоп, и
            # именно на глубоких вариантах пол начинает срабатывать. Если бы
            # я его забыл, глубокий вход показал бы RR, которого в торговле
            # не бывает.
            if entry - stop < entry * min_stop:
                stop = entry * (1 - min_stop)
            target = end + size * config.TP1_LEVEL
        else:
            entry = end + size * depth
            if not (end <= s['price'] <= entry):
                continue
            stop = end + size * config.SL_LEVEL_R + size * config.SL_BUFFER
            if stop - entry < entry * min_stop:
                stop = entry * (1 + min_stop)
            target = end - size * config.TP1_LEVEL

        distance = abs(entry - stop)
        if distance <= 0:
            continue
        rr = abs(target - entry) / distance
        if rr < min_rr:
            continue

        orders.append(Order(
            pair=s['pair'], direction=s['type'],
            entry=entry, stop=stop, targets=[target], fractions=[1.0],
            created=s['created'], expires=s['expires'], key=s['key'],
            be_trigger=end if config.BREAKEVEN_AT_B else None,
            meta={'rr': rr, 'direction': s['type'],
                  'stop_pct': distance / entry * 100},
        ))
    return orders


def run_variant(setups, data, depth, sides, min_rr):
    import config
    from smc_engine import compute_stats, run_portfolio

    orders = build_orders(setups, depth, sides, min_rr)
    if len(orders) < 3:
        return None
    result = run_portfolio(
        orders, {p: data[p]['5m'] for p in data},
        risk_pct=config.RISK_PER_TRADE,
        max_positions=getattr(config, 'MAX_OPEN_POSITIONS', 5),
        cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
        max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0),
        breakeven_after_tp1=False)

    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 3:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    # Заявка — это объект Order, а не словарь: у него meta полем, а не по
    # ключу. Сбор статистики стоял в самом конце, поэтому весь дорогой поиск
    # сетапов успевал пройти и обрушиться на последней строке.
    planned = np.array([(o.meta or {}).get('rr', 0) for o in orders], dtype=float)
    stop_pct = np.array([(o.meta or {}).get('stop_pct', 0) for o in orders],
                        dtype=float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'fill': len(trades) / len(orders) * 100,
            'mean': float(r.mean()), 'total': float(r.sum()),
            'wr': float((r > 0).mean() * 100),
            'rr': float(np.median(planned)), 'stop': float(np.median(stop_pct)),
            'dd': stats['max_dd_pct'], 'ret': stats['return_pct']}


def load(cache_dir, pairs, label):
    import config

    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data = {}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is not None:
            data[pair] = loaded

    # Сканируем с САМЫМ ГЛУБОКИМ входом: его окно шире всех, и окна остальных
    # вариантов получаются из него отбором. Порог RR снят — он применяется
    # потом, по варианту.
    saved = (config.ZONE_A_TOP, config.MIN_RR)
    config.ZONE_A_TOP = 1.0 - DEEPEST
    config.MIN_RR = 0.0
    quiet = hush()
    setups = []
    try:
        for pair in data:
            setups += collect_setups(pair, data[pair])
            print(f'      {pair}: сетапов всего {len(setups)}', flush=True)
    finally:
        unhush(quiet)
        config.ZONE_A_TOP, config.MIN_RR = saved
    return data, setups


BOTH = ('LONG', 'SHORT')
SHORT = ('SHORT',)

VARIANTS = [
    ('вход 38.2% (как сейчас)',   0.382, BOTH),
    ('вход 50.0%',                0.500, BOTH),
    ('вход 61.8% (дальний край)', 0.618, BOTH),
    ('шорты · вход 38.2%',        0.382, SHORT),
    ('шорты · вход 50.0%',        0.500, SHORT),
    ('шорты · вход 61.8%',        0.618, SHORT),
]


def main():
    import config

    print('Арифметика, известная до замера: RR = (0.25 + r) / (0.896 - r)')
    for depth in (0.382, 0.5, 0.618):
        print(f'   вход {depth * 100:.1f}%  →  RR {(0.25 + depth) / (0.896 - depth):.2f}')

    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, setups) in periods.items():
        print()
        print('=' * 116)
        print(f'{label}   импульсов найдено: {len(setups)}')
        print('=' * 116)
        head = (f'{"вариант":<28}{"заявок":>8}{"сделок":>8}{"набралось":>11}'
                f'{"винрейт":>9}{"RR план":>9}{"стоп %":>8}{"R/сделку":>10}'
                f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, depth, sides in VARIANTS:
            res = run_variant(setups, data, depth, sides, config.MIN_RR)
            if res is None:
                print(f'{name:<28}{"— сделок нет":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<28}{res["orders"]:>8}{res["n"]:>8}{res["fill"]:>10.0f}%'
                  f'{res["wr"]:>8.1f}%{res["rr"]:>9.2f}{res["stop"]:>8.2f}'
                  f'{res["mean"]:>10.3f}{res["total"]:>9.1f}{res["ret"]:>9.1f}'
                  f'{res["dd"]:>7.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    base = 'вход 38.2% (как сейчас)'
    print()
    print('=' * 116)
    print('СРАВНЕНИЕ С НЫНЕШНИМ ВХОДОМ (интервал разницы средних)')
    print('=' * 116)
    head = f'{"вариант":<28}' + ''.join(f'{lbl:>34}' for lbl in results)
    print(head)
    print('-' * len(head))
    for name, _d, _s in VARIANTS:
        if name == base:
            continue
        cells, verdicts = '', []
        for label, table in results.items():
            res, ref = table.get(name), table.get(base)
            if not res or not ref:
                cells += f'{"—":>34}'
                verdicts.append(False)
                continue
            lo, hi = diff_ci(res['r'], ref['r'])
            gain = res['mean'] - ref['mean']
            cell = f'{gain:+.3f} [{lo:+.3f}; {hi:+.3f}] n={res["n"]}'
            cells += f'{cell:>34}'
            verdicts.append(gain > 0 and lo > 0)
        mark = '  ЛУЧШЕ на обоих' if all(verdicts) and verdicts else ''
        print(f'{name:<28}{cells}{mark}')

    print()
    print('Столбец «набралось» — какая доля выставленных лимитов дождалась цены.')
    print('Именно он и есть цена глубокого входа: RR там выше по построению,')
    print('а вот доедет ли до него коррекция — вопрос к данным.')


if __name__ == '__main__':
    main()
