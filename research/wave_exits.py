"""
Волновая стратегия: разбор выходов и попытка вытащить её ведением позиции.

ОТКУДА ЗАДАЧА — ИЗ АРИФМЕТИКИ, КОТОРАЯ НЕ СХОДИТСЯ. В основном замере базовый
вариант дал винрейт 37.9% при плановом отношении риска к прибыли 2.1. Отсюда
ожидаемый результат:

    0.379 × 2.1 − 0.621 × 1.0 = +0.175 R

А замер показал валовых +0.036 R. Разрыв 0.139 R — вчетверо больше самого
края. Значит сделки не доходят до заявленной геометрии, и подозреваемый не
разметка, а выход: предел удержания в 96 баров закрывает позицию раньше цели,
а выход по времени на +0.1 R считается «победой» в винрейте, не принося денег.

ЭТО НЕ ОПРАВДАНИЕ ВОЛН. Это означает, что мерилось не то, что задумывалось, и
прежде чем закрывать тему, надо померить задуманное.

ДЫРА В ПРЕДЫДУЩЕМ ЗАМЕРЕ, КОТОРУЮ НАДО ЗАКРЫТЬ. Вариант «цель 1.0» показал
«мало сделок», и это было прочитано как результат. Это не результат, а ноль
заявок: при входе на 50% и стопе за началом волны 1 отношение риска к прибыли
равно ровно 1.0, а фильтр MIN_RR стоял на 1.5 — все заявки отсеялись до
прогона. Ближняя цель, единственная способная поправить арифметику выше, не
проверялась вообще. Здесь MIN_RR снижен до 0.8 там, где это нужно варианту.

ЧТО ГОВОРЯТ ПРАКТИКИ (внешние источники, не наша выдумка):
    · частичная фиксация 50-75% на расширении 1.618 волны 1, остаток — в
      расчёте на волну 5;
    · стоп подтягивается под каждый мелкий откат ВНУТРИ волны 3;
    · вход в коррекции волны 2 или волны 4 ради движения в волне 3.
Первые два — про ведение позиции, и оба проверяются здесь.

ЧЕСТНО ПРО МНОЖЕСТВЕННЫЙ ПЕРЕБОР. Это уже третий замер по одной теме на тех же
данных, и суммарно проверено больше тридцати вариантов. При таком переборе
«лучший» вариант почти наверняка окажется лучшим случайно. Защита ровно одна и
она та же, что и всегда: требование пройти на ДВУХ независимых периодах сразу.
Вероятность случайного попадания при этом умножается, а не складывается —
0.05 × 0.05 вместо 0.05. Ослаблять это правило под конец темы было бы
подгонкой, а не улучшением.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА И НЕ ПОДЛЕЖАЩАЯ СМЯГЧЕНИЮ:

    в плюсе на ОБОИХ периодах, доверительный интервал не накрывает ноль
    И просадка не больше 25% на обоих.

РЕЗУЛЬТАТ: РАЗБОР ВЫХОДОВ ОПРОВЕРГ ГИПОТЕЗУ, РАДИ КОТОРОЙ ЗАМЕР ЗАТЕВАЛСЯ.

    чем кончилась      доля    R/сделку
    стоп                57%      −1.056
    цель                27%      +2.017
    по времени          17%      +0.338

Никакого разрыва нет: 0.27 × 2.017 + 0.57 × (−1.056) + 0.17 × 0.338 = 0.000.
Ошибка была в самой постановке — «винрейт 37.9%» считает ВСЕ сделки с
положительным результатом, а до цели доходит только 27%. Остальные «победы» —
выходы по времени на +0.34 R. Сделки не «не дотягивают до своей геометрии»,
они просто недостаточно часто попадают.

ВТОРАЯ ЦИФРА, КОТОРУЮ НАДО ЗАПОМНИТЬ: стоп стоит −1.056 R, а не −1.0
(проскальзывание плюс комиссии). Безубыток поэтому требует не 33.3%, а
1.056 / (2.017 + 1.056) = 34.4% попаданий. Стратегия даёт 27%.

САМОЕ ЧЕСТНОЕ ЧИСЛО ВО ВСЁМ ЗАМЕРЕ. Медианный ход в пользу позиции 1.00 R,
против — 1.03 R (на медведе 0.89 и 1.04). Типичная сделка одинаково далеко
ходит в обе стороны. Направленной информации в разметке нет, и это видно на
уровне отдельной сделки, без всяких интервалов и без ссылок на издержки.

ЧТО ЗАКРЫТО ЭТИМ ПРОГОНОМ
    · ближняя цель, не проверявшаяся раньше из-за фильтра MIN_RR: попаданий
      становится 51-56%, R на сделку падает до нуля. Обмен один в один;
    · рецепты практиков (частичная фиксация 50/75% на 1.618, безубыток,
      подтяжка стопа) — ни один не прошёл оба периода;
    · трейлинг ВРЕДИТ: ход в пользу позиции падает с 1.13 до 0.69 R, то есть
      подтяжка вырезает сделки раньше, чем они доходят;
    · удержание 240 и 480 баров вместо 96 сдвигает результат, но в РАЗНЫЕ
      стороны на разных порогах разметки.

ЧТО РЕШАЕТ ДЕЛО ОКОНЧАТЕЛЬНО. Периоды выбирают РАЗНЫХ победителей: лучший на
быке («половина на 1.618, дальше безубыток», +0.089) даёт на медведе +0.028 при
просадке 29%, а лучший на медведе («держим 480», +0.071) даёт на быке +0.057
при просадке 30%. И сам порог разметки переворачивает знак всей медвежьей
колонки: при 1.5 положительны почти все варианты, при 2.5 — отрицательны все.
Одни и те же сетапы, другая зернистость разметки, противоположный знак.

ИТОГ ПО ТЕМЕ: три замера, около пятидесяти вариантов, принято ноль.

ПРО СТАРШИЙ ТАЙМФРЕЙМ, КОТОРЫЙ НАПРАШИВАЕТСЯ СЛЕДУЮЩИМ. Теория фрактальна, и
проверить четырёхчасовой масштаб кажется естественным. Но ось масштаба уже
пройдена порогом зигзага от 0.75 до 3.5 ATR, и КРУПНЫЙ масштаб оказался
худшим: при 3.5 медианное колено 6 ATR, запаздывание 9 баров, результат −0.037
и −0.044. Это тот же вопрос, заданный другими словами, и ответ на него уже
получен.

Запуск:
    python research/wave_exits.py
"""

import os
import sys
from collections import Counter

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wave_impulse as wi  # noqa: E402
from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

# Порог 1.5 — единственный, где основной замер дал плюс на обоих периодах,
# и 2.5 как база оттуда же. Мелкую сетку порогов уже прошли, она шум.
wi.THRESHOLDS = (1.5, 2.5)

BAR_MIN = 60

# Геометрия выхода. Ключи:
#   targets  — список расширений волны 1 от её начала;
#   fracs    — какая доля позиции снимается на каждой цели;
#   be       — переводить ли в безубыток после первой цели;
#   trail    — трейлинг в ATR (0 — выключен);
#   hold     — предел удержания в барах;
#   min_rr   — фильтр геометрии (нужен, чтобы ближняя цель вообще прошла).
BASE_EXIT = {'targets': [1.618], 'fracs': [1.0], 'be': False,
             'trail': 0.0, 'hold': 96, 'min_rr': 1.5}

VARIANTS = [
    ('база: одна цель 1.618 · держим 96 баров', {}),

    # ── Закрываем дыру: ближняя цель, которая не проверялась ────────────────
    ('цель 1.0 — конец волны 1 (фильтр RR снят)', {'targets': [1.0],
                                                   'min_rr': 0.8}),
    ('цель 1.272',                                {'targets': [1.272],
                                                   'min_rr': 0.8}),

    # ── Ответ на разрыв арифметики: дать сделке дойти ───────────────────────
    ('держим 240 баров',                          {'hold': 240}),
    ('держим 480 баров',                          {'hold': 480}),

    # ── Рецепт практиков: частичная фиксация ───────────────────────────────
    ('половина на 1.0, остаток на 1.618',         {'targets': [1.0, 1.618],
                                                   'fracs': [0.5, 0.5],
                                                   'min_rr': 0.8}),
    ('половина на 1.618, остаток на 2.618',       {'targets': [1.618, 2.618],
                                                   'fracs': [0.5, 0.5]}),
    ('три четверти на 1.618, остаток на 2.618',   {'targets': [1.618, 2.618],
                                                   'fracs': [0.75, 0.25]}),

    # ── Безубыток после первой цели ────────────────────────────────────────
    ('половина на 1.0 · дальше безубыток',        {'targets': [1.0, 1.618],
                                                   'fracs': [0.5, 0.5],
                                                   'be': True, 'min_rr': 0.8}),
    ('половина на 1.618 · дальше безубыток',      {'targets': [1.618, 2.618],
                                                   'fracs': [0.5, 0.5],
                                                   'be': True}),

    # ── Трейлинг вместо цели ───────────────────────────────────────────────
    ('трейлинг 2 ATR, цель 2.618',                {'targets': [2.618],
                                                   'trail': 2.0}),
    ('трейлинг 3 ATR, цель 2.618',                {'targets': [2.618],
                                                   'trail': 3.0}),
    ('трейлинг 3 ATR · держим 240',               {'targets': [2.618],
                                                   'trail': 3.0, 'hold': 240}),

    # ── Всё сразу, если по отдельности что-то помогло ──────────────────────
    ('половина на 1.0 · трейлинг 3 ATR · 240',    {'targets': [1.0, 2.618],
                                                   'fracs': [0.5, 0.5],
                                                   'trail': 3.0, 'hold': 240,
                                                   'min_rr': 0.8}),
]

THRESHOLD_RUNS = (1.5, 2.5)


def build_orders(marks, exit_cfg, threshold):
    """Заявки с заданной геометрией выхода."""
    from smc_engine import Order
    from wave import core, params

    cfg = dict(wi.BASE, threshold=threshold)
    life = np.timedelta64(params.EXPIRY_BARS * BAR_MIN * 60, 's')
    targets = exit_cfg['targets']
    orders = []

    for mark in marks:
        pivots, atr = mark['pivots'], mark['atr']
        close, stamps = mark['close'], mark['stamps']
        for k in range(len(pivots)):
            wave = core.find_wave(
                pivots, k, atr,
                entry_mode=cfg['entry_mode'], min_wave_atr=cfg['min_wave_atr'],
                min_leg_ratio=cfg['min_leg_ratio'],
                min_retrace=cfg['min_retrace'], max_retrace=cfg['max_retrace'])
            if wave is None:
                continue
            at = wave['at']
            if at >= len(close) - 2:
                continue
            # Геометрия строится по ПЕРВОЙ цели: она решает, годится ли сетап.
            trade = core.build_trade(
                wave, price_now=close[at], entry_retrace=cfg['entry_retrace'],
                target_ext=targets[0], min_rr=exit_cfg['min_rr'])
            if trade is None:
                continue

            long_side = wave['direction'] == 'LONG'
            base = wave['a']['price']
            prices = [base + wave['wave1'] * ext if long_side
                      else base - wave['wave1'] * ext for ext in targets]
            created = stamps[at]
            orders.append(Order(
                pair=mark['pair'], direction=wave['direction'],
                entry=trade['entry'], stop=trade['stop'],
                targets=prices, fractions=list(exit_cfg['fracs']),
                created=created, expires=created + life,
                key=(mark['pair'], at, k), entry_type='limit',
                trail_distance=(exit_cfg['trail'] * wave['atr']
                                if exit_cfg['trail'] else None),
                meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                      'direction': wave['direction']},
            ))
    return orders


def run(marks, data, exit_cfg, threshold):
    from smc_engine import compute_stats, run_portfolio
    from wave import params

    orders = build_orders(marks, exit_cfg, threshold)
    if len(orders) < 20:
        return None
    result = run_portfolio(
        orders, data, risk_pct=params.RISK_PCT,
        max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=exit_cfg['be'],
        max_hold_hours=exit_cfg['hold'] * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 20:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    reasons = Counter(t.get('exit_reason', '?') for t in trades)
    by_reason = {}
    for name in reasons:
        picked = [t['pnl'] / t['risk'] for t in trades
                  if t.get('exit_reason', '?') == name]
        by_reason[name] = (len(picked), float(np.mean(picked)))
    mfe = np.array([t.get('mfe_r', 0) for t in trades], float)
    mae = np.array([t.get('mae_r', 0) for t in trades], float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'reasons': by_reason, 'mfe': float(np.median(mfe)),
            'mae': float(np.median(mae))}


def autopsy(res, label):
    """Чем кончаются сделки — то, ради чего замер и затевался."""
    print(f'\nРАЗБОР ВЫХОДОВ · {label}')
    print(f'{"чем кончилась":<18}{"сделок":>8}{"доля":>8}{"R/сделку":>10}'
          f'{"вклад в сумму":>15}')
    print('-' * 59)
    total = res['n']
    for name, (count, mean) in sorted(res['reasons'].items(),
                                      key=lambda kv: -kv[1][0]):
        print(f'{name:<18}{count:>8}{count / total:>7.0%}{mean:>10.3f}'
              f'{count * mean:>15.1f}')
    print(f'{"ИТОГО":<18}{total:>8}{1:>7.0%}{res["mean"]:>10.3f}'
          f'{res["total"]:>15.1f}')
    print(f'ход в пользу позиции (медиана): {res["mfe"]:.2f} R   '
          f'против: {res["mae"]:.2f} R')
    print('Если сделок с TIME_STOP много, а R у них около нуля — цель просто')
    print('не успевает быть достигнутой, и плановое отношение риска к прибыли')
    print('никогда не реализуется. Тогда лечится удержанием или целью ближе.')


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = wi.scan(pairs, cache, label)

    results = {}
    for label, (data, marks) in periods.items():
        print()
        print('=' * 122)
        print(f'{label}   пар: {len(data)}')
        print('=' * 122)

        base = run(marks[2.5], data, BASE_EXIT, 2.5)
        if base:
            autopsy(base, f'{label} · база, порог 2.5')

        for threshold in THRESHOLD_RUNS:
            print()
            head = (f'{"вариант · порог " + str(threshold):<44}{"заявок":>8}'
                    f'{"сделок":>8}{"винрейт":>9}{"MFE":>7}{"R вал.":>9}'
                    f'{"издер.":>8}{"R/сделку":>10}{"сумма":>8}{"DD%":>7}'
                    f'{"интервал":>22}')
            print(head)
            print('-' * len(head))
            for name, override in VARIANTS:
                cfg = dict(BASE_EXIT, **override)
                res = run(marks[threshold], data, cfg, threshold)
                key = (name, threshold)
                if res is None:
                    print(f'{name:<44}{"— мало сделок":>16}')
                    continue
                results.setdefault(label, {})[key] = res
                lo, hi = ci(res['r'])
                print(f'{name:<44}{res["orders"]:>8}{res["n"]:>8}'
                      f'{res["wr"]:>8.1f}%{res["mfe"]:>7.2f}{res["gross"]:>9.3f}'
                      f'{res["costs"]:>8.3f}{res["mean"]:>10.3f}'
                      f'{res["total"]:>8.1f}{res["dd"]:>7.1f}'
                      f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 122)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на ОБОИХ периодах,')
    print('интервал не накрывает ноль И просадка не больше 25% на обоих.')
    print('Это третий замер по теме на тех же данных; двусторонняя проверка —')
    print('единственная защита от того, что «лучший» вариант лучший случайно.')
    print('=' * 122)
    labels = list(results)
    passed = []
    for threshold in THRESHOLD_RUNS:
        for name, _ in VARIANTS:
            cells, ok = '', []
            for label in labels:
                res = results[label].get((name, threshold))
                if not res:
                    cells += f'{"—":>36}'
                    ok.append(False)
                    continue
                lo, hi = ci(res['r'])
                ok.append(res['mean'] > 0 and lo > 0 and res['dd'] <= 25)
                cell = f'{res["mean"]:+.3f} [{lo:+.3f}] DD {res["dd"]:.0f}%'
                cells += f'{cell:>36}'
            good = bool(ok) and all(ok)
            if good:
                passed.append((name, threshold))
            print(f'{name[:40]:<40}{threshold:>6}{cells}'
                  f'{"  ПРИНЯТ" if good else ""}')
        print('-' * 122)

    print()
    if passed:
        print('ПРИНЯТО:')
        for name, threshold in passed:
            print(f'   · {name} (порог {threshold})')
    else:
        print('НЕ ПРИНЯТО НИ ОДНОГО ВАРИАНТА.')
        print('Если при этом разбор выходов показал, что цель достигалась')
        print('редко, а исправление удержания результат не сдвинуло — значит')
        print('дело не в выходе, и тема закрыта окончательно.')


if __name__ == '__main__':
    main()
