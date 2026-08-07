"""
Волновая стратегия Эллиотта: вход в начале волны 3.

ЧТО ПРОВЕРЯЕТСЯ, СФОРМУЛИРОВАННОЕ УЗКО. Не «работает ли теория Эллиотта» — так
вопрос не ставится, потому что теория целиком не проверяема: две трети её правил
требуют уже состоявшихся волн. Проверяется единственное утверждение, которое
можно принять или отвергнуть в момент решения:

    отбор импульса по волновой разметке делает ДАЛЬНЮЮ цель работающей
    там, где на произвольном импульсе она не работает.

Вторая половина этой фразы — не предположение, а наш собственный замер: дальние
цели у сетки Фибоначчи уже мерились, цель 0.50 дала −0.007 и −0.032 R, дальше
хуже. Канонические 1.618 от начала волны 1 попадают ровно в этот диапазон.
Значит если волновой отбор ничего не добавляет, результат будет тем же самым, и
это будет ответ.

ЧТО ГОВОРИТ ПРОБНИК (research/wave_probe.py, 57 601 час, 6 пар) — три числа,
и все три против теории:

    запаздывание разметки          ~50% длины колена на ЛЮБОМ пороге
    правило 1 нарушено             46-49% троек, то есть почти монетка
    откат попал в канон 0.5-0.618  18-21%, медиана глубже: 0.63-0.71

Первое — признанная слабость метода: подтверждение волны 1 стоит половины её
времени. Второе означает, что рыночные колебания примерно симметричны, а не
чередуются «импульс — коррекция», как утверждает теория. Третье означает, что
канонические уровни Фибоначчи описывают пятую часть случаев.

Замер всё равно проводится: пробник меряет ЧАСТОТУ структуры, а не её
прибыльность, а это разные вещи. Редкая структура может быть выгодной.

ДВА РЕЖИМА ВХОДА — ЭТО ДВА РАЗНЫХ УТВЕРЖДЕНИЯ, И ОНИ МЕРЯЮТСЯ ПОРОЗНЬ:
    лимит  — заявка на уровне отката сразу после подтверждения волны 1.
             Лучшая цена, но разметка ещё не подтверждена: волна 2 может уйти
             за начало волны 1 и отменить сетап уже после налива.
    рынок  — вход, когда дно волны 2 подтверждено пивотом. Разметка налицо, но
             цена уже ушла на порог разворота вверх: стоп шире, цель та же.
Первое — это по сути наша FIBO с другим отбором. Второе — собственно Эллиотт.

ПРИЁМКА ЗАПИСАНА ДО ПРОГОНА И СМЯГЧЕНИЮ НЕ ПОДЛЕЖИТ:

    в плюсе на ОБОИХ периодах, доверительный интервал не накрывает ноль
    И просадка не больше 25% на обоих.

Порог просадки здесь не формальность. Профиль сделки — редкие крупные попадания
при отношении риска к прибыли 2-3, то есть винрейт около 30%. На таком винрейте
череда из десяти убытков подряд — рядовое событие, и стратегия с просадкой 40%
означает не «повезёт в следующий раз», а разорение до следующего раза.

РЕЗУЛЬТАТ ПРОГОНА: НИ ОДИН ИЗ 14 ВАРИАНТОВ НЕ ПРИНЯТ. Валовый край везде в
пределах ±0.1 R при издержках 0.03-0.07 R, то есть чистого края нет. Ни на
одном варианте и ни на одном периоде интервал не отделился от нуля — при
выборках в 550-2100 сделок это означает не «мало данных», а отсутствие эффекта.

ТРИ ВЕЩИ ИЗ ЭТОЙ ТАБЛИЦЫ СТОИТ ЗАПОМНИТЬ, ЧТОБЫ НЕ ПЕРЕПРОВЕРЯТЬ.

1. ЗАКОНОМЕРНОСТИ ПО ПОРОГУ НЕТ — и это исправление, а не вывод. По трём
   точкам (1.5 → 2.5 → 3.5) виделось монотонное падение на обоих периодах, из
   чего напрашивалось «за подтверждение разметки платят больше, чем получают».
   Мелкая сетка порогов в research/wave_threshold.py это опровергла: кривая
   валового края идёт 0.076 / 0.053 / 0.049 / 0.086 / 0.140 / 0.036 / −0.004,
   и максимум у периодов в разных местах. Три точки были срезом шума.

   Урок общий и дороже самого замера: монотонность на трёх точках — не
   закономерность, а то, как выглядит шум, если смотреть на него редко.

2. Канонический вариант — худший из всех. Откат строго 0.5-0.618, как в
   учебнике: −0.037 на быке и −0.216 на медведе, худшая строка таблицы. Канон
   Фибоначчи описывает 18-21% откатов (см. пробник), и отбор по нему выбрасывает
   четыре пятых сетапов, не улучшая оставшиеся.

3. Подтверждённая разметка (вход по рынку) не окупается: бык +0.092, медведь
   −0.065. Знак меняется между периодами — это шум, а не край.

ЧТО ОСТАЛОСЬ ОТКРЫТЫМ и вынесено в research/wave_threshold.py: единственная
строка, положительная на обоих периодах, — самый мелкий порог, где волновой
структуры фактически нет. Отдельный замер выясняет, край это или продолжение
той же монотонной кривой.

Запуск:
    python research/wave_impulse.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

PAIRS_LIMIT = 10
BAR_MIN = 60

# Пороги зигзага, вокруг которых строится всё. Взяты из пробника: 1.5 даёт
# 294 пивота на 1000 баров при запаздывании в 1 бар, 3.5 — 44 пивота при
# запаздывании 9 баров. Это и есть тот самый размен «шум против опоздания».
THRESHOLDS = (1.5, 2.5, 3.5)

BASE = {
    'threshold': 2.5,
    'entry_mode': 'limit',
    'entry_retrace': 0.5,
    'target_ext': 1.618,
    'min_wave_atr': 3.0,
    'min_leg_ratio': 0.0,
    'min_retrace': 0.382,
    'max_retrace': 0.90,
}

#   имя варианта, что меняем относительно базы
VARIANTS = [
    ('база: порог 2.5 · лимит 50% · цель 1.618', {}),
    ('порог 1.5',                                {'threshold': 1.5}),
    ('порог 3.5',                                {'threshold': 3.5}),
    ('вход по рынку (разметка подтверждена)',    {'entry_mode': 'market'}),
    ('по рынку · порог 1.5',                     {'entry_mode': 'market',
                                                  'threshold': 1.5}),
    ('по рынку · порог 3.5',                     {'entry_mode': 'market',
                                                  'threshold': 3.5}),
    ('цель 1.0 (конец волны 1)',                 {'target_ext': 1.0}),
    ('цель 2.618',                               {'target_ext': 2.618}),
    ('вход 38.2%',                               {'entry_retrace': 0.382}),
    ('вход 61.8%',                               {'entry_retrace': 0.618}),
    ('канон: откат только 0.5-0.618',            {'entry_mode': 'market',
                                                  'min_retrace': 0.5,
                                                  'max_retrace': 0.618}),
    ('волна 1 крупнее предыдущего колена',       {'min_leg_ratio': 1.0}),
    ('волна 1 от 5 ATR',                         {'min_wave_atr': 5.0}),
    ('по рынку · волна 1 от 5 ATR',              {'entry_mode': 'market',
                                                  'min_wave_atr': 5.0}),
]


def scan(pairs, cache_dir, label):
    """Зигзаги всех пар на всех порогах. Считается один раз на период."""
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt
    from wave import core

    print(f'[{label}] загрузка и разметка...', flush=True)
    data, marks = {}, {thr: [] for thr in THRESHOLDS}
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
        stamps = stamps.to_numpy()
        atr = core.atr_series(high, low, close)
        for thr in THRESHOLDS:
            pivots = core.zigzag(high, low, close, reversal_atr=thr, atr=atr)
            marks[thr].append({'pair': pair, 'pivots': pivots, 'atr': atr,
                               'close': close, 'stamps': stamps})
        shown = THRESHOLDS[len(THRESHOLDS) // 2]
        print(f'      {pair}: пивотов при {shown} — '
              f'{len(marks[shown][-1]["pivots"])}', flush=True)
    return data, marks


def build_orders(marks, cfg):
    from smc_engine import Order
    from wave import core, params

    life_bars = params.EXPIRY_BARS if cfg['entry_mode'] == 'limit' else 1
    life = np.timedelta64(life_bars * BAR_MIN * 60, 's')

    orders, waves = [], 0
    for mark in marks:
        pivots, atr = mark['pivots'], mark['atr']
        close, stamps = mark['close'], mark['stamps']
        for k in range(len(pivots)):
            wave = core.find_wave(
                pivots, k, atr,
                entry_mode=cfg['entry_mode'],
                min_wave_atr=cfg['min_wave_atr'],
                min_leg_ratio=cfg['min_leg_ratio'],
                min_retrace=cfg['min_retrace'],
                max_retrace=cfg['max_retrace'])
            if wave is None:
                continue
            waves += 1
            at = wave['at']
            if at >= len(close) - 2:
                continue
            trade = core.build_trade(
                wave, price_now=close[at],
                entry_retrace=cfg['entry_retrace'],
                target_ext=cfg['target_ext'])
            if trade is None:
                continue
            created = stamps[at]
            orders.append(Order(
                pair=mark['pair'], direction=wave['direction'],
                entry=trade['entry'], stop=trade['stop'],
                targets=[trade['target']], fractions=[1.0],
                created=created, expires=created + life,
                key=(mark['pair'], at, k),
                # Вход по рынку — это тейкер; лимит на откате — мейкер.
                entry_type='stop' if cfg['entry_mode'] == 'market' else 'limit',
                meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                      'retrace': wave['retrace'], 'wave1_atr': wave['wave1_atr'],
                      'lag': wave['lag'], 'direction': wave['direction']},
            ))
    return orders, waves


def run(marks, data, cfg):
    from smc_engine import compute_stats, run_portfolio
    from wave import params

    orders, waves = build_orders(marks, cfg)
    if len(orders) < 20:
        return None
    result = run_portfolio(
        orders, data,
        risk_pct=params.RISK_PCT,
        max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=cfg.get('max_same_direction',
                                   params.MAX_SAME_DIRECTION),
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 20:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    rr = np.array([(o.meta or {}).get('rr', 0) for o in orders], float)
    lag = np.array([(o.meta or {}).get('lag', 0) for o in orders], float)
    wave1 = np.array([(o.meta or {}).get('wave1_atr', 0) for o in orders], float)
    longs = sum(1 for o in orders if o.direction == 'LONG')
    return {'r': r, 'n': len(trades), 'orders': len(orders), 'waves': waves,
            'fill': len(trades) / len(orders) * 100,
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'rr': float(np.median(rr)), 'lag': float(np.median(lag)),
            'wave1': float(np.median(wave1)),
            'longs': longs / len(orders) * 100}


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = scan(pairs, cache, label)

    results = {}
    for label, (data, marks) in periods.items():
        print()
        print('=' * 128)
        print(f'{label}   пар: {len(data)}')
        print('=' * 128)
        head = (f'{"вариант":<42}{"волн":>8}{"заявок":>8}{"сделок":>8}'
                f'{"налив":>7}{"RR":>6}{"лаг":>5}{"лонг":>6}{"винрейт":>9}'
                f'{"R вал.":>9}{"издер.":>8}{"R/сделку":>10}{"сумма":>8}'
                f'{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, override in VARIANTS:
            cfg = dict(BASE, **override)
            res = run(marks[cfg['threshold']], data, cfg)
            if res is None:
                print(f'{name:<42}{"— мало сделок":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<42}{res["waves"]:>8}{res["orders"]:>8}{res["n"]:>8}'
                  f'{res["fill"]:>6.0f}%{res["rr"]:>6.1f}{res["lag"]:>5.0f}'
                  f'{res["longs"]:>5.0f}%{res["wr"]:>8.1f}%{res["gross"]:>9.3f}'
                  f'{res["costs"]:>8.3f}{res["mean"]:>10.3f}{res["total"]:>8.1f}'
                  f'{res["dd"]:>7.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 128)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на ОБОИХ периодах,')
    print('интервал не накрывает ноль И просадка не больше 25% на обоих.')
    print('=' * 128)
    for name, _ in VARIANTS:
        cells, ok = '', []
        for label, table in results.items():
            res = table.get(name)
            if not res:
                cells += f'{"—":>36}'
                ok.append(False)
                continue
            lo, hi = ci(res['r'])
            ok.append(res['mean'] > 0 and lo > 0 and res['dd'] <= 25)
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}] DD {res["dd"]:.0f}%'
            cells += f'{cell:>36}'
        print(f'{name:<42}{cells}{"  ПРИНЯТ" if ok and all(ok) else ""}')

    print()
    print('КАК ЧИТАТЬ. «R вал.» — край до издержек: если он отрицателен, идея')
    print('не работает, и комиссии ни при чём. «лаг» — сколько баров прошло')
    print('между экстремумом и моментом, когда разметка стала известна; это')
    print('плата за объективность счёта, и она видна прямо в таблице.')
    print('Сравнение режимов входа отвечает на главный вопрос теории: стоит ли')
    print('подтверждённая разметка той цены, которую за неё платят.')


if __name__ == '__main__':
    main()
