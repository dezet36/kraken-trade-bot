"""
Возврат после резкого движения: есть ли край, если платить мейкера.

ПОЧЕМУ ЭТО НЕ ПЯТАЯ ПОПЫТКА ОДНОГО И ТОГО ЖЕ. Четыре закрытые идеи за день
роднит одно: все они платили тейкера с обеих сторон. При стопе 0.5% это
0.42 R с каждой сделки, и требуемый валовый край получался таким, какого у
коротких горизонтов просто не бывает.

    круг издержек        тейкер оба   мейкер вход + тейкер стоп   мейкер оба
    при стопе 0.5%           0.42 R                     0.25 R       0.08 R

Здесь вход ЛИМИТОМ против движения — он исполняется мейкером по построению:
импульс сам приходит к заявке, догонять её не надо. Цель тоже лимит. Тейкера
платит только стоп. Движок это различает: комиссия входа зависит от типа
заявки, цели считаются по мейкеру, стопы по тейкеру.

Способ входа меняет издержки впятеро. Ни один фильтр отбора, проверенный за
сегодня, столько не двигал — четыре признака у пробоя дали сотые доли R.

ПОРОГ ИМПУЛЬСА ВЗЯТ ИЗ ДАННЫХ. Отношение «движение за 6 баров к ATR одного»
на 5-минутках BTC: медиана 1.11, 90-й процентиль 2.71, 95-й 3.30. Порог 3.0 —
это 93-й процентиль, около 2800 сетапов на 40 тысяч баров. Назначать его на
глаз я уже пробовал на прижатии: получил значение ниже пятого процентиля и
один сетап на двадцать тысяч баров.

ЧТО МЕНЯЕТСЯ, ПО ОДНОЙ ОСИ ЗА РАЗ. Размер импульса, глубина лимита, цель,
стоп. Плюс отдельный контрольный вариант: та же геометрия, но вход
стоп-ордером вместо лимита. Он и покажет, сколько на самом деле стоит
способ входа — а не сколько я насчитал в уме.

ПРИЁМКА. Двусторонняя: в плюсе на ОБОИХ периодах и интервал не накрывает
ноль. Фильтров отбора здесь нет намеренно: сначала меряется голая идея.
Фильтры добавляют к тому, что работает, а не к тому, что надо спасти.

Запуск:
    python research/revert_scalp.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

PAIRS_LIMIT = 6
BAR_MIN = 5

# Пороги импульса, под которые нужен отдельный поиск: они меняют сам набор
# сетапов, а не только геометрию сделки.
IMPULSES = (2.5, 3.0, 4.0)

# Геометрия строится из готовых сетапов, поиск для неё не повторяется.
#   имя, порог импульса, глубина лимита, доля цели, стоп в ATR, тип входа
# ВТОРОЙ ЗАХОД, ПО СЛЕДУ ПЕРВОГО. Первый прогон дал единственную находку за
# день: валовый край ПОЛОЖИТЕЛЕН на обоих периодах и идёт монотонным
# градиентом по близости цели —
#
#     цель 0.3 -> +0.105 и +0.089      винрейт 53-54%
#     цель 0.5 -> -0.057 и -0.052      винрейт 41%
#     цель 0.8 -> -0.108 и -0.107      винрейт 34%
#
# Это не случайный победитель из девяти: направление одинаково на двух
# независимых периодах и объяснимо механически — чем ближе цель, тем чаще она
# берётся. Чистый результат при этом -0.017 и -0.031, и весь разрыв до нуля —
# издержки в 0.12 R.
#
# Отсюда две оси, и обе бьют в издержки. Ещё более близкая цель поднимает
# валовый край дальше. Более широкий стоп уменьшает объём позиции при том же
# риске, а значит и издержки В ЕДИНИЦАХ R — но снижает отношение риска к
# прибыли. Что перевесит, арифметика не скажет.
VARIANTS = [
    ('цель 0.30 · стоп 1.0 (найдено)',        3.0, 0.3, 0.30, 1.0, 'limit'),
    ('цель 0.25',                             3.0, 0.3, 0.25, 1.0, 'limit'),
    ('цель 0.20',                             3.0, 0.3, 0.20, 1.0, 'limit'),
    ('цель 0.15',                             3.0, 0.3, 0.15, 1.0, 'limit'),
    ('цель 0.30 · стоп 2.0 ATR',              3.0, 0.3, 0.30, 2.0, 'limit'),
    ('цель 0.30 · стоп 3.0 ATR',              3.0, 0.3, 0.30, 3.0, 'limit'),
    ('цель 0.20 · стоп 2.0 ATR',              3.0, 0.3, 0.20, 2.0, 'limit'),
    ('цель 0.20 · стоп 3.0 ATR',              3.0, 0.3, 0.20, 3.0, 'limit'),
    ('цель 0.25 · стоп 2.0 · импульс 2.5',    2.5, 0.3, 0.25, 2.0, 'limit'),
    ('цель 0.25 · стоп 2.0 · импульс 4.0',    4.0, 0.3, 0.25, 2.0, 'limit'),
]


def collect(pair, df, impulse_atr):
    """Сетапы одной пары при заданном пороге импульса."""
    from revert import core, params

    saved = params.IMPULSE_ATR
    params.IMPULSE_ATR = impulse_atr
    try:
        open_ = df['open'].to_numpy(float)
        high = df['high'].to_numpy(float)
        low = df['low'].to_numpy(float)
        close = df['close'].to_numpy(float)
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        stamps = stamps.to_numpy()

        out = []
        for i in range(60, len(close) - 1):
            setup = core.find_setup(open_, high, low, close, i)
            if setup:
                setup['pair'] = pair
                setup['at'] = i
                setup['time'] = stamps[i]
                out.append(setup)
        return out
    finally:
        params.IMPULSE_ATR = saved


def build_orders(setups, offset, target_frac, stop_atr, entry_type):
    from revert import core, params
    from smc_engine import Order

    life = np.timedelta64(params.EXPIRY_BARS * BAR_MIN * 60, 's')
    orders = []
    for s in setups:
        # Глубина лимита — часть сетапа, поэтому вход пересчитываем здесь, а
        # не берём готовый: иначе вариант «лимит на экстремуме» мерился бы на
        # входах базового варианта и отличался бы от него только названием.
        extreme, atr_now = s['extreme'], s['atr']
        entry = (extreme + offset * atr_now if s['direction'] == 'SHORT'
                 else extreme - offset * atr_now)
        # Порог RR снят: близкая цель с широким стопом даёт отношение
        # ниже единицы ПО ПОСТРОЕНИЮ, и штатный порог вырезал бы ровно
        # те варианты, ради которых замер и делается. Достаточно ли
        # частых попаданий при низком RR — вопрос к данным, а не к порогу.
        trade = core.build_trade({**s, 'entry': entry}, stop_atr,
                                 target_frac, min_rr=0.0)
        if trade is None:
            continue
        created = s['time']
        orders.append(Order(
            pair=s['pair'], direction=s['direction'],
            entry=trade['entry'], stop=trade['stop'],
            targets=[trade['target']], fractions=[1.0],
            created=created, expires=created + life,
            key=(s['pair'], s['direction'], int(s['at'])),
            entry_type=entry_type,
            meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                  'direction': s['direction']},
        ))
    return orders


def run(setups, data, offset, target_frac, stop_atr, entry_type):
    from revert import params
    from smc_engine import compute_stats, run_portfolio

    orders = build_orders(setups, offset, target_frac, stop_atr, entry_type)
    if len(orders) < 5:
        return None
    result = run_portfolio(
        orders, data,
        risk_pct=params.RISK_PCT, max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 5:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'fill': len(trades) / len(orders) * 100,
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct']}


def load(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data, setups = {}, {imp: [] for imp in IMPULSES}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or '5m' not in loaded:
            continue
        df = loaded['5m']
        data[pair] = df
        for imp in IMPULSES:
            setups[imp] += collect(pair, df, imp)
        print(f'      {pair}: сетапов при 3.0 — {len(setups[3.0])}', flush=True)
    return data, setups


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, setups) in periods.items():
        print()
        print('=' * 116)
        print(f'{label}   сетапов при пороге 3.0: {len(setups[3.0])}')
        print('=' * 116)
        head = (f'{"вариант":<38}{"заявок":>8}{"сделок":>8}{"набрал":>8}'
                f'{"винрейт":>9}{"R вал.":>9}{"издержки":>10}{"R/сделку":>10}'
                f'{"сумма R":>9}{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, imp, offset, target, stop, kind in VARIANTS:
            res = run(setups[imp], data, offset, target, stop, kind)
            if res is None:
                print(f'{name:<38}{"— мало сделок":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<38}{res["orders"]:>8}{res["n"]:>8}{res["fill"]:>7.0f}%'
                  f'{res["wr"]:>8.1f}%{res["gross"]:>9.3f}{res["costs"]:>10.3f}'
                  f'{res["mean"]:>10.3f}{res["total"]:>9.1f}{res["dd"]:>7.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 116)
    print('ПРИЁМКА: в плюсе на ОБОИХ периодах и интервал не накрывает ноль')
    print('=' * 116)
    for name, *_rest in VARIANTS:
        cells, ok = '', []
        for label, table in results.items():
            res = table.get(name)
            if not res:
                cells += f'{"—":>32}'
                ok.append(False)
                continue
            lo, hi = ci(res['r'])
            ok.append(res['mean'] > 0 and lo > 0)
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}; {hi:+.3f}] n={res["n"]}'
            cells += f'{cell:>32}'
        mark = '  ПРИНЯТ' if all(ok) and ok else ''
        print(f'{name:<38}{cells}{mark}')

    print()
    print('ЧТО СМОТРЕТЬ. Столбец «R вал.» — край ДО издержек. Если он растёт с')
    print('приближением цели и с расширением стопа, а чистый переходит ноль —')
    print('идея живая. Если валовый край падает при близкой цели, значит первый')
    print('прогон нашёл случайность, и тему надо закрывать.')


if __name__ == '__main__':
    main()
