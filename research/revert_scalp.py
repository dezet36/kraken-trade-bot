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

ИТОГ ТРЁХ ЗАХОДОВ, 2026-08-06. КРАЙ ЕСТЬ. ТОРГОВАТЬ ЕГО НЕЛЬЗЯ.

    вариант                бык R     DD    медведь R     DD
    цель 0.20 · стоп 3.0  +0.039   9.9%      +0.007   47.5%
    цель 0.20 · стоп 4.0  +0.034   8.2%      +0.011   38.6%
    цель 0.20 · стоп 5.0  +0.027   6.5%      +0.013   28.4%
    цель 0.20 · стоп 6.0  +0.024   7.0%      +0.011   25.9%

Интервалы не накрывают ноль на обоих периодах у семи вариантов из девяти.
Это первый положительный результат за весь день и единственный за четыре
закрытые идеи. Но правило, записанное ДО прогона, включало и просадку не
больше 25% на обоих периодах — и его не проходит НИ ОДИН.

ПОЧЕМУ ПРОСАДКА ЗДЕСЬ НЕ ПРИДИРКА. Профиль сделки: винрейт 85-92%,
отношение риска к прибыли около 0.15-0.2. Безубыток требует 83% попаданий,
измерено 85-90% — запас три-семь процентных пунктов. Это подбирание монет:
работает, пока убытки не собираются в кучу, и первая же кучка съедает
накопленное. Просадка 26-48% на медвежьем периоде и есть такая кучка.

И ГЛАВНОЕ — КАК ИМЕННО УЛУЧШАЛСЯ РЕЗУЛЬТАТ. Просадка падала с расширением
стопа: 47.5 -> 38.6 -> 28.4 -> 25.9 при стопе 3 -> 4 -> 5 -> 6 ATR. Но
валовый край при этом таял: 0.073 -> 0.064 -> 0.056 -> 0.048. То есть мы
улучшали цифры не тем, что находили предсказание рынка, а тем, что убирали
контроль риска: стоп в шесть ATR при цели в 0.6 ATR почти не срабатывает.
Продолжать по этому градиенту — идти к позиции без стопа вообще, и метрики
будут улучшаться до самого конца, ровно до первого гэпа.

ЧТО ЗДЕСЬ ВСЁ-ТАКИ ПОДТВЕРДИЛОСЬ. Арифметика издержек: расширение стопа
уменьшает их вдвое (0.121 -> 0.064 R), и это ровно то, что считалось на
бумаге. И направление: возврат после резкого движения ИМЕЕТ положительный
валовый край на обоих периодах, в отличие от пробоя, где он был
отрицательным до всяких издержек.

ЧЕГО ПРОВЕРИТЬ НЕ УДАЛОСЬ. Контрольный вариант «тот же вход стоп-ордером»
задумывался как измерение цены способа входа и не измерил ничего: у него
доля исполненных 37% против 25% и винрейт 58% против 41%, то есть это другой
набор сделок. Довод «мейкерский вход экономит впятеро» остался расчётом.

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
    # ТРЕТИЙ И ПОСЛЕДНИЙ ЗАХОД. Второй подтвердил обе оси, и обе — ровно так,
    # как предсказывала арифметика издержек:
    #
    #     стоп 1.0 -> 2.0 -> 3.0 ATR   издержки 0.121 -> 0.092 -> 0.069 R
    #     чистый край (медведь)        -0.058 -> -0.032 -> -0.020
    #
    # Лучшее на сегодня — цель 0.20 при стопе 3.0: +0.039 на бычьем периоде и
    # +0.007 на медвежьем. Нижняя граница интервала на медвежьем ровно 0.000,
    # то есть приёмку он НЕ проходит, но подходит к ней вплотную.
    #
    # ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ НА САМОМ ДЕЛЕ. Не «можно ли ещё подвинуть», а не
    # упирается ли улучшение в стену. Отношение риска к прибыли уже 0.2:
    # цель 0.20 от импульса это около 0.6 ATR против стопа в 3 ATR, и
    # безубыток требует 83% попаданий при измеренных 85-88%. Запас в три
    # процентных пункта на распределении с тяжёлым хвостом — это не стратегия,
    # это подбирание монет.
    #
    # Поэтому решение записано ДО прогона: принимается только вариант, у
    # которого интервал не накрывает ноль на ОБОИХ периодах И просадка не
    # больше 25% на обоих. Улучшение чистого края ценой просадки в половину
    # депозита улучшением не считается.
    ('цель 0.20 · стоп 3.0 (лучшее)',         3.0, 0.3, 0.20, 3.0, 'limit'),
    ('цель 0.20 · стоп 4.0',                  3.0, 0.3, 0.20, 4.0, 'limit'),
    ('цель 0.20 · стоп 5.0',                  3.0, 0.3, 0.20, 5.0, 'limit'),
    ('цель 0.20 · стоп 6.0',                  3.0, 0.3, 0.20, 6.0, 'limit'),
    ('цель 0.25 · стоп 4.0',                  3.0, 0.3, 0.25, 4.0, 'limit'),
    ('цель 0.25 · стоп 5.0',                  3.0, 0.3, 0.25, 5.0, 'limit'),
    ('цель 0.15 · стоп 4.0',                  3.0, 0.3, 0.15, 4.0, 'limit'),
    ('импульс 2.5 · цель 0.20 · стоп 4.0',    2.5, 0.3, 0.20, 4.0, 'limit'),
    ('импульс 2.5 · цель 0.25 · стоп 5.0',    2.5, 0.3, 0.25, 5.0, 'limit'),
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
    print('ПРАВИЛО, ЗАПИСАННОЕ ДО ПРОГОНА: интервал не накрывает ноль на ОБОИХ')
    print('периодах И просадка не больше 25% на обоих. Второе условие тут не')
    print('придирка: при отношении риска к прибыли около 0.2 безубыток требует')
    print('83% попаданий, и запас над ним — три процентных пункта. На таком')
    print('запасе просадка в половину депозита означает, что стратегия живёт')
    print('до первой кластеризации убытков, а не до конца периода.')
    print()
    for label, table in results.items():
        best = [(v['mean'], n, v) for n, v in table.items()]
        if not best:
            continue
        best.sort(reverse=True)
        top = best[0]
        print(f'{label}: лучший «{top[1]}» — {top[0]:+.3f} R, '
              f'просадка {top[2]["dd"]:.1f}%, сделок {top[2]["n"]}')


if __name__ == '__main__':
    main()
