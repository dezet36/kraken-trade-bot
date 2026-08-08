"""
Полная разметка Эллиотта 1-2-3-4-5 против ПЛАЦЕБО со случайными пивотами.

ЧТО ЗДЕСЬ НОВОГО ПО СРАВНЕНИЮ С ПРЕЖНИМИ ТРЕМЯ ЗАМЕРАМИ. Раньше проверялся
единственный сетап — вход в начале волны 3, где правила 2 и 3 неприменимы в
принципе: волн 3, 4 и 5 ещё нет. Отвергнута была ОДНА конструкция, а не теория.

Здесь размечается импульс целиком по окну из шести пивотов, применяются все три
строгих правила и добавлена уверенность разметки по таблице Фибоначчи. Сетап
другой: вход в конце волны 4 с расчётом на волну 5.

ГЛАВНОЕ В ЭТОМ ЗАМЕРЕ — НЕ ВАРИАНТЫ, А КОНТРОЛЬ. Источник требует его прямо:

    «Обязательно сравните результат с плацебо-версией: та же логика входа и
    выхода, но пивоты для разметки взяты случайно. Если волновая версия не
    превосходит случайную заметно и стабильно — скорее всего, вы находите
    иллюзорные закономерности. Это стандартный, обязательный контроль именно
    для pattern-matching методов вроде Elliott Wave.»

Мы его не гоняли ни разу, а он решает всё. Любой положительный результат
разметки без такого контроля необъясним: три правила и скоринг Фибоначчи
отбирают ФОРМУ движения, а форма коррелирует с волатильностью и трендом сама по
себе. Плацебо отвечает на вопрос «а не то же ли самое дают любые шесть точек с
такой же геометрией».

КАК УСТРОЕНО ПЛАЦЕБО. Берутся те же бары, то же число пивотов и то же
распределение расстояний между ними, но САМИ пивоты расставлены случайно —
разметка теряет связь с ценой, сохраняя статистику. Дальше всё одинаково: те же
правила, тот же скоринг, тот же вход, тот же выход, тот же движок и те же
издержки. Отличается ровно одна вещь — осмысленность точек.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    1. в плюсе на ОБОИХ периодах и интервал не накрывает ноль;
    2. просадка не больше 25% на обоих;
    3. И РЕЗУЛЬТАТ ЗАМЕТНО ВЫШЕ ПЛАЦЕБО: интервал разницы с ним не накрывает
       ноль на обоих периодах.

Третий пункт не смягчается ни при каких результатах первых двух. Стратегия,
неотличимая от случайной разметки, не является волновой стратегией, какой бы
ни была её доходность.

РЕЗУЛЬТАТ: ПРИНЯТО НОЛЬ — И ЭТО САМЫЙ ИНФОРМАТИВНЫЙ ПРОГОН ПО ТЕМЕ.

РАЗМЕТКА НАХОДИТ НАСТОЯЩУЮ СТРУКТУРУ, и плацебо это доказывает:

                              реальные пивоты   случайные
    уверенность по Фибоначчи      0.27-0.47      0.07-0.10
    волна 3 растянута              67-93%          9-28%

Разница огромна и устойчива во всех клетках. Три правила и таблица соотношений
описывают рынок ВЕРНО: размеченные импульсы действительно ложатся в
канонические пропорции, и третья волна действительно растянута чаще прочих —
ровно как утверждает источник.

И ЭТА СТРУКТУРА НЕ ПРЕДСКАЗЫВАЕТ ЦЕНУ.

    вариант                        бык       медведь
    полная разметка, порог 2.5   −0.300      −0.140
    порог 1.5                    +0.027      +0.037
    порог 3.5                    +0.170      −0.470
    уверенность Фибоначчи ≥0.3   −0.462      +0.171

Полная разметка со всеми тремя правилами оказалась ХУЖЕ прежней версии по двум
пивотам. Знак пляшет между периодами и порогами.

ПРО ПЛАЦЕБО НАДО ЧИТАТЬ ВНИМАТЕЛЬНО. Разница с ним: −0.293 при пороге 2.5,
+0.450 и +0.764 при 1.5, −0.095 и −0.512 при 3.5. Знак непостоянен. А там, где
разметка «побеждает» (порог 1.5), она делает это не своим качеством: реальная
даёт +0.027 и +0.037, то есть ноль, а плацебо проваливается в −0.422 и −0.727.
Обыграть плохое плацебо, стоя на нуле, — не доказательство края.

ЧЕСТНО ПРО МОЩНОСТЬ. Полная разметка требует шести подтверждённых пивотов и
даёт 100-150 сделок против 900-2100 у версии по двум. Интервалы вышли ±0.3-0.5,
и мелкий край такой замер увидеть не смог бы в принципе. Это ограничение
данных, а не вывод. Но точечные оценки в большинстве клеток отрицательные, а не
«слегка положительные и неразличимые».

ОБЩИЙ ИТОГ ПО ВОЛНОВОЙ ТЕМЕ: четыре замера, около шестидесяти вариантов,
принято ноль. Теория верна как КЛАССИФИКАЦИЯ уже случившегося движения и не
работает как предсказание следующего. Это согласуется и с §7 самого источника:
рецензируемых подтверждений нет, а автоматические разметчики дают кандидатов на
разметку, а не торговые сигналы.

Запуск:
    python research/wave_impulse5.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from common import ci, diff_ci  # noqa: E402

PAIRS_LIMIT = 10
BAR_MIN = 60
THRESHOLDS = (1.5, 2.5, 3.5)
RNG = np.random.default_rng(20260807)

BASE = {
    'threshold': 2.5, 'min_wave_atr': 3.0, 'min_score': 0.0,
    'target_mode': 'equality', 'stop_pad': 0.25, 'placebo': False,
}

VARIANTS = [
    ('полная разметка, порог 2.5',        {}),
    ('  то же на случайных пивотах',      {'placebo': True}),

    ('порог 1.5',                         {'threshold': 1.5}),
    ('  то же на случайных пивотах',      {'threshold': 1.5, 'placebo': True}),

    ('порог 3.5',                         {'threshold': 3.5}),
    ('  то же на случайных пивотах',      {'threshold': 3.5, 'placebo': True}),

    ('уверенность Фибоначчи от 0.3',      {'min_score': 0.3}),
    ('  то же на случайных пивотах',      {'min_score': 0.3, 'placebo': True}),

    ('уверенность Фибоначчи от 0.5',      {'min_score': 0.5}),
    ('цель 61.8% волны 1',                {'target_mode': 'w1_618'}),
    ('цель 38.2% хода волн 1-3',          {'target_mode': 'net_382'}),
    ('волна 1 от 5 ATR',                  {'min_wave_atr': 5.0}),
]

# Какие варианты с каким плацебо сравнивать.
PAIRED = {
    'полная разметка, порог 2.5': 0,
    'порог 1.5': 2,
    'порог 3.5': 4,
    'уверенность Фибоначчи от 0.3': 6,
}


def shuffle_pivots(pivots, n_bars):
    """
    Плацебо: столько же пивотов, те же промежутки — но точки случайны.

    Сохраняется ЧИСЛО пивотов и распределение расстояний между ними, теряется
    только привязка к цене. Иначе сравнение было бы нечестным: разметка с
    другим числом точек даёт другое число сетапов, и разница объяснялась бы
    этим, а не осмысленностью.

    Цена случайного пивота берётся с бара, куда он попал, — иначе цены
    перестали бы соответствовать графику и правила отсеяли бы всё подряд.
    """
    if len(pivots) < 2:
        return []
    gaps = np.diff([p['index'] for p in pivots])
    gaps = RNG.permutation(gaps)
    out, at = [], int(pivots[0]['index'])
    kind = pivots[0]['kind']
    lag = int(np.median([p['confirmed_at'] - p['index'] for p in pivots]))
    for gap in gaps:
        at += int(gap)
        if at >= n_bars - 2:
            break
        out.append({'index': at, 'kind': kind,
                    'confirmed_at': min(at + lag, n_bars - 1)})
        kind = 'H' if kind == 'L' else 'L'
    return out


def scan(pairs, cache_dir, label):
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
        atr = core.atr_series(high, low, close)
        for thr in THRESHOLDS:
            pivots = core.zigzag(high, low, close, reversal_atr=thr, atr=atr)
            fake = shuffle_pivots(pivots, len(close))
            # Цена случайного пивота — с его бара, чтобы разметка оставалась
            # согласованной с графиком.
            for point in fake:
                i = point['index']
                point['price'] = float(high[i] if point['kind'] == 'H'
                                       else low[i])
            marks[thr].append({'pair': pair, 'pivots': pivots, 'fake': fake,
                               'atr': atr, 'close': close,
                               'stamps': stamps.to_numpy()})
        print(f'      {pair}: пивотов при 2.5 — '
              f'{len(marks[2.5][-1]["pivots"])}', flush=True)
    return data, marks


def build_orders(marks, cfg):
    from smc_engine import Order
    from wave import impulse as imp, params

    life = np.timedelta64(1 * BAR_MIN * 60, 's')
    orders, found = [], 0
    key = 'fake' if cfg['placebo'] else 'pivots'
    for mark in marks:
        pivots, atr = mark[key], mark['atr']
        close, stamps = mark['close'], mark['stamps']
        for k in range(len(pivots)):
            wave = imp.find_impulse(pivots, k, atr,
                                    min_wave_atr=cfg['min_wave_atr'])
            if wave is None:
                continue
            if wave['score'] < cfg['min_score']:
                continue
            found += 1
            at = wave['at']
            if at >= len(close) - 2:
                continue
            trade = imp.wave_four_entry(
                wave, stop_pad_atr=cfg['stop_pad'],
                target_mode=cfg['target_mode'], price_now=close[at])
            if trade is None:
                continue
            created = stamps[at]
            orders.append(Order(
                pair=mark['pair'], direction=wave['direction'],
                entry=trade['entry'], stop=trade['stop'],
                targets=[trade['target']], fractions=[1.0],
                created=created, expires=created + life,
                key=(mark['pair'], at, k), entry_type='stop',
                meta={'rr': trade['rr'], 'score': wave['score'],
                      'lag': wave['lag'], 'extended': wave['extended']}))
    return orders, found


def run(marks, data, cfg):
    from smc_engine import compute_stats, run_portfolio
    from wave import params

    orders, found = build_orders(marks, cfg)
    if len(orders) < 20:
        return None
    result = run_portfolio(
        orders, data, risk_pct=params.RISK_PCT,
        max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 20:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    score = np.array([(o.meta or {}).get('score', 0) for o in orders], float)
    lag = np.array([(o.meta or {}).get('lag', 0) for o in orders], float)
    ext = [(o.meta or {}).get('extended') for o in orders]
    return {'r': r, 'n': len(trades), 'orders': len(orders), 'found': found,
            'mean': float(r.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'score': float(np.mean(score)), 'lag': float(np.median(lag)),
            'w3': ext.count('w3') / max(len(ext), 1) * 100}


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = scan(pairs, cache, label)

    results = {}
    for label, (data, marks) in periods.items():
        print()
        print('=' * 122)
        print(f'{label}   пар: {len(data)}')
        print('=' * 122)
        head = (f'{"вариант":<38}{"размеч.":>9}{"заявок":>8}{"сделок":>8}'
                f'{"увер.":>7}{"лаг":>5}{"3-я раст.":>11}{"винрейт":>9}'
                f'{"R/сделку":>10}{"сумма":>8}{"DD%":>7}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for i, (name, override) in enumerate(VARIANTS):
            cfg = dict(BASE, **override)
            res = run(marks[cfg['threshold']], data, cfg)
            results[label][i] = res
            if res is None:
                print(f'{name:<38}{"— мало сделок":>16}')
                continue
            lo, hi = ci(res['r'])
            print(f'{name:<38}{res["found"]:>9}{res["orders"]:>8}{res["n"]:>8}'
                  f'{res["score"]:>7.2f}{res["lag"]:>5.0f}{res["w3"]:>10.0f}%'
                  f'{res["wr"]:>8.1f}%{res["mean"]:>10.3f}{res["total"]:>8.1f}'
                  f'{res["dd"]:>7.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 122)
    print('ГЛАВНАЯ ПРОВЕРКА: РАЗМЕТКА ПРОТИВ СЛУЧАЙНЫХ ПИВОТОВ')
    print('Если разница неотличима от нуля — три правила и скоринг Фибоначчи')
    print('не несут информации, и стратегия не является волновой, какой бы ни')
    print('была её доходность.')
    print('=' * 122)
    labels = list(results)
    head = f'{"вариант":<38}' + ''.join(f'{lab:>40}' for lab in labels)
    print(head)
    print('-' * len(head))
    for name, placebo_at in PAIRED.items():
        index = [i for i, (n, _) in enumerate(VARIANTS) if n == name][0]
        cells = ''
        for label in labels:
            real = results[label].get(index)
            fake = results[label].get(placebo_at + 1)
            if not real or not fake:
                cells += f'{"—":>40}'
                continue
            lo, hi = diff_ci(real['r'], fake['r'])
            gap = real['mean'] - fake['mean']
            cells += f'{f"{gap:+.3f} [{lo:+.3f}; {hi:+.3f}]":>40}'
        print(f'{name:<38}{cells}')

    print()
    print('=' * 122)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на обоих периодах, интервал')
    print('не накрывает ноль, просадка не больше 25% И разница с плацебо тоже')
    print('отделена от нуля на обоих периодах.')
    print('=' * 122)
    for name, placebo_at in PAIRED.items():
        index = [i for i, (n, _) in enumerate(VARIANTS) if n == name][0]
        ok = []
        for label in labels:
            real = results[label].get(index)
            fake = results[label].get(placebo_at + 1)
            if not real or not fake:
                ok.append(False)
                continue
            lo, _hi = ci(real['r'])
            dlo, _dhi = diff_ci(real['r'], fake['r'])
            ok.append(real['mean'] > 0 and lo > 0 and real['dd'] <= 25
                      and dlo > 0)
        print(f'{name:<38}{"  ПРИНЯТ" if ok and all(ok) else "  не принят"}')


if __name__ == '__main__':
    main()
