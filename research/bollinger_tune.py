"""
Боллинджер: подбор параметров с ОТЛОЖЕННЫМИ парами.

ЗАЧЕМ ОТДЕЛЬНАЯ ЗАЩИТА ИМЕННО ЗДЕСЬ. Стратегия принята в бумагу как кандидат с
краем +0.025 и +0.050 R, и весь этот край — 2.3 и 3.8 процентных пункта
винрейта сверх безубытка. Подбирать параметры на тех же двух периодах, которые
этот край и породили, — прямой путь к подгонке: при двадцати вариантах лучший
окажется лучшим случайно с заметной вероятностью, а размер случайности здесь
как раз сопоставим с размером края.

Двусторонняя приёмка (оба периода) от этого защищает лишь отчасти: варианты
подбираются, ГЛЯДЯ на оба периода сразу, и потому оба перестают быть
независимой проверкой.

ПОЭТОМУ ВПЕРВЫЕ В ПРОЕКТЕ ДЕЛАЕТСЯ НАСТОЯЩАЯ ЗАДЕРЖКА ВЫБОРКИ. Пул делится
пополам ПО ПАРАМ, и половины не пересекаются:

    настройка — на первой половине, все варианты видны;
    проверка  — на второй, которую подбор не видел ни разу.

Деление по парам, а не по времени, выбрано сознательно: разрезав период
пополам, мы получили бы два отрезка одного и того же рынка, и «проверка»
унаследовала бы его особенности. Разные инструменты — независимее.

Это стало возможно только сейчас: пул вырос до 21 пары. Раньше половины были
бы по четыре пары, и обе оказались бы слишком шумными.

ЧТО ПРОВЕРЯЕТСЯ. Оси, которых не касались ни разу:

    период и множитель полос   учебные 20 и 2.0 взяты как есть, а именно они
                               определяют, что вообще считается сетапом;
    порог RSI в обратном       пробник показал монотонность: >45 даёт 55%
    режиме                     возвратов, >50 уже 76%. На 50 и остановились;
    ширина стопа               проверены 0.5 и 1.0, причём 1.0 сильно лучше.
                               1.5 удешевляет круг комиссий ещё на треть, но
                               роняет отношение риска к прибыли ниже единицы;
    четырёхчасовой график      издержки падают ещё вдвое, сделок вчетверо
                               меньше — при нынешних 730 запас есть.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА:

    вариант принимается, только если на ОТЛОЖЕННОЙ половине он в плюсе на
    ОБОИХ периодах И не хуже нынешней настройки на той же половине.

Результат на половине настройки не является основанием ни для чего: он служит
только для того, чтобы выбрать, что нести на проверку.

РЕЗУЛЬТАТ: ПРИНЯТ МНОЖИТЕЛЬ ПОЛОС 2.5 ВМЕСТО УЧЕБНЫХ 2.0.

             бык·настр.  медв.·настр.  бык·пров.  медв.·пров.  сделок (пров.)
    2.0 σ      +0.024      +0.113       +0.027     −0.022       395 / 360
    2.5 σ      +0.139      +0.116       +0.128     +0.063       163 / 145
    винрейт     60.8%       59.1%        59.5%      57.2%       (было 53-60%)

Положителен во всех четырёх клетках. При отношении риска к прибыли 1.0 весь
край — это запас винрейта сверх безубытка, и он растёт с ~2 до ~5 процентных
пунктов. Цена: сделок вдвое меньше.

ПОЧЕМУ НЕ ВЗЯТ «ПЕРИОД 30 · 2.5 σ», ХОТЯ ОН ЛУЧШИЙ ПО ОЦЕНКЕ. Он дал +0.272 и
+0.253 на проверке — и −0.011 на половине НАСТРОЙКИ, куда сводная таблица не
смотрит, при 33-39 сделках в клетке. Ровно за этим четыре клетки и нужны:
сводка по проверочной половине показала бы его лучшим.

ЗАГЛЯДЫВАНИЕ ВПЕРЁД, НАЙДЕННОЕ ЗДЕСЬ ЖЕ. Первый прогон дал четырёхчасовому
варианту +0.610 и +0.370 R на сделку — больше, чем у любой принятой стратегии
проекта. Причина была не в стратегии: отметка свечи это её ОТКРЫТИЕ, сигнал
считается по ЗАКРЫТИЮ, а движок начинает исполнение с бара, следующего за
created. При часовом сигнале на часовом исполнении это совпадает само собой;
при четырёхчасовом заявка наливалась ВНУТРИ своей же свечи. После починки тот
же вариант даёт −0.027 и −0.262, то есть весь эффект был утечкой.
Защита вынесена в тест на самом движке: Live_Bot/tests/test_signal_bar_timing.

ЧЕСТНО О СТРОГОСТИ ЭТОГО ЗАМЕРА. Приёмка здесь слабее обычной проектной: в ней
нет требования, чтобы интервал не накрывал ноль. По обычному стандарту не
проходит ни один вариант, включая принятый. В пользу 2.5 σ говорит не интервал,
а четыре независимые положительные клетки — случайно так выходит в одном случае
из шестнадцати.

Запуск:
    python research/bollinger_tune.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

POOL = 20
BASE = {
    'tf': '1h', 'bb_period': 20, 'bb_mult': 2.0, 'rsi_period': 14,
    'rsi_edge': 50.0, 'stop_frac': 1.0, 'target_frac': 1.0,
}

VARIANTS = [
    ('нынешняя настройка', {}),

    # ── Основание сетапа: сами полосы. Ни разу не трогали ──────────────────
    ('полосы 2.5 сигмы',              {'bb_mult': 2.5}),
    ('полосы 1.5 сигмы',              {'bb_mult': 1.5}),
    ('период полос 30',               {'bb_period': 30}),
    ('период полос 50',               {'bb_period': 50}),
    ('период 30 · 2.5 сигмы',         {'bb_period': 30, 'bb_mult': 2.5}),

    # ── Порог расхождения: пробник показал монотонность ────────────────────
    ('порог RSI 55',                  {'rsi_edge': 55.0}),
    ('порог RSI 60',                  {'rsi_edge': 60.0}),
    ('порог RSI 55 · 2.5 сигмы',      {'rsi_edge': 55.0, 'bb_mult': 2.5}),

    # ── Период RSI ────────────────────────────────────────────────────────
    ('RSI период 7',                  {'rsi_period': 7}),
    ('RSI период 21',                 {'rsi_period': 21}),

    # ── Геометрия: стоп шире удешевляет круг, но роняет RR ─────────────────
    ('стоп 1.5 полуширины',           {'stop_frac': 1.5}),
    ('стоп 1.5 · цель 1.5',           {'stop_frac': 1.5, 'target_frac': 1.5}),
    ('цель 0.7 (не доходя середины)', {'target_frac': 0.7}),

    # ── Старший масштаб: издержки падают ещё вдвое ─────────────────────────
    ('четыре часа',                   {'tf': '4h'}),
    ('четыре часа · порог RSI 55',    {'tf': '4h', 'rsi_edge': 55.0}),
]


def scan(pairs, cache_dir, label):
    """Свечи всех пар на обоих масштабах. Индикаторы считаются позже."""
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка {len(pairs)} пар...', flush=True)
    out = {}
    for pair in pairs:
        loaded = bt.load_pair(pair)
        if loaded is None:
            continue
        frames = {}
        for tf in ('1h', '4h'):
            df = loaded.get(tf)
            if df is None or len(df) < 200:
                continue
            stamps = pd.to_datetime(df['timestamp'])
            if getattr(stamps.dt, 'tz', None) is not None:
                stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
            frames[tf] = (df, stamps.to_numpy())
        if '1h' in frames:
            out[pair] = frames
    return out


def build(frames, cfg):
    """Заявки одного варианта по заранее загруженным свечам."""
    from rsibb import core, params
    from smc_engine import Order

    bar_min = {'1h': 60, '4h': 240}[cfg['tf']]
    life = np.timedelta64(params.EXPIRY_BARS * bar_min * 60, 's')
    gap = max(4, params.MAX_HOLD_BARS // 4)

    # СМЕЩЕНИЕ ВРЕМЕНИ СОЗДАНИЯ — НЕ МЕЛОЧЬ, А ЗАЩИТА ОТ ЗАГЛЯДЫВАНИЯ ВПЕРЁД.
    # Отметка свечи — это её ОТКРЫТИЕ, а сигнал считается по её ЗАКРЫТИЮ.
    # Движок начинает исполнение с бара, следующего за created.
    #
    # При часовом сигнале и часовом исполнении это совпадает само собой:
    # следующий бар открывается ровно тогда, когда закрытие стало известно.
    # При ЧЕТЫРЁХЧАСОВОМ сигнале и часовом исполнении — нет: исполнение
    # начиналось бы через час после открытия четырёхчасовой свечи, то есть
    # ВНУТРИ неё, а лимит стоял бы на полосе, о касании которой мы узнаём лишь
    # через три часа.
    #
    # Первый прогон дал на этом +0.610 R на сделку — больше, чем у любой
    # принятой стратегии проекта. Такие числа здесь всегда означали ошибку.
    #
    # Сдвигаем created на последний ЧАСОВОЙ бар внутри сигнальной свечи: тогда
    # исполнение начинается с первого бара ПОСЛЕ её закрытия.
    shift = np.timedelta64((bar_min - 60) * 60, 's')
    orders = []

    for pair, per_tf in frames.items():
        if cfg['tf'] not in per_tf:
            continue
        df, stamps = per_tf[cfg['tf']]
        ind = core.indicators(df['open'].to_numpy(float),
                              df['high'].to_numpy(float),
                              df['low'].to_numpy(float),
                              df['close'].to_numpy(float),
                              bb_period=cfg['bb_period'],
                              bb_mult=cfg['bb_mult'],
                              rsi_period=cfg['rsi_period'])
        last = -10 ** 9
        for i in range(cfg['bb_period'] + 40, len(ind['close']) - 2):
            if i - last < gap:
                continue
            setup, _why = core.evaluate(
                ind, i, rsi_mode='divergence',
                rsi_low=cfg['rsi_edge'], rsi_high=100.0 - cfg['rsi_edge'],
                adx_max=0.0, max_width_ratio=0.0, entry_mode='touch')
            if setup is None:
                continue
            trade = core.build_trade(setup, target_frac=cfg['target_frac'],
                                     stop_frac=cfg['stop_frac'],
                                     thin_stop='widen')
            if trade is None:
                continue
            last = i
            created = stamps[i] + shift
            orders.append(Order(
                pair=pair, direction=setup['direction'],
                entry=trade['entry'], stop=trade['stop'],
                targets=[trade['target']], fractions=[1.0],
                created=created, expires=created + life,
                key=(pair, cfg['tf'], i), entry_type='limit',
                meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct']}))
    return orders


def run(frames, cfg):
    from rsibb import params
    from smc_engine import compute_stats, run_portfolio

    orders = build(frames, cfg)
    if len(orders) < 30:
        return None
    # Исполнение ВСЕГДА на часовых свечах, даже когда сигнал четырёхчасовой:
    # иначе стоп и цель внутри одной большой свечи разрешались бы грубее, чем
    # у остальных вариантов, и сравнение перестало бы быть честным.
    exec_data = {pair: per_tf['1h'][0] for pair, per_tf in frames.items()}
    bar_min = {'1h': 60, '4h': 240}[cfg['tf']]
    result = run_portfolio(
        orders, exec_data, risk_pct=params.RISK_PCT,
        max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * bar_min / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 30:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'rr': float(np.median([(o.meta or {}).get('rr', 0) for o in orders]))}


def table(title, results, names):
    print()
    print('=' * 118)
    print(title)
    print('=' * 118)
    head = (f'{"вариант":<34}{"заявок":>8}{"сделок":>8}{"RR":>6}{"винрейт":>9}'
            f'{"R вал.":>9}{"издер.":>8}{"R/сделку":>10}{"сумма":>8}'
            f'{"DD%":>7}{"интервал":>22}')
    print(head)
    print('-' * len(head))
    for name in names:
        res = results.get(name)
        if not res:
            print(f'{name:<34}{"— мало сделок":>16}')
            continue
        lo, hi = ci(res['r'])
        print(f'{name:<34}{res["orders"]:>8}{res["n"]:>8}{res["rr"]:>6.2f}'
              f'{res["wr"]:>8.1f}%{res["gross"]:>9.3f}{res["costs"]:>8.3f}'
              f'{res["mean"]:>10.3f}{res["total"]:>8.1f}{res["dd"]:>7.1f}'
              f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}')


def main():
    names = [name for name, _ in VARIANTS]
    halves = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        usable = pairs[:POOL]
        cut = len(usable) // 2
        # Чётные в настройку, нечётные в проверку: пары в списке отсортированы
        # по обороту, и деление «первые/вторые» отдало бы настройке крупные
        # инструменты, а проверке мелкие. Тогда провал на проверке значил бы
        # «на мелких не работает», а не «подгонка».
        tune = scan(usable[0::2], cache, f'{label} · настройка')
        verify = scan(usable[1::2], cache, f'{label} · проверка')
        halves[label] = (tune, verify)
        print(f'   {label}: настройка {len(tune)} пар, проверка {len(verify)}',
              flush=True)

    outcome = {}
    for label, (tune, verify) in halves.items():
        for part, frames in (('настройка', tune), ('проверка', verify)):
            got = {}
            for name, override in VARIANTS:
                got[name] = run(frames, dict(BASE, **override))
            outcome[(label, part)] = got
            table(f'{label} · {part} ({len(frames)} пар)', got, names)

    print()
    print('=' * 118)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: вариант принимается только если на')
    print('ОТЛОЖЕННОЙ половине он в плюсе на ОБОИХ периодах И не хуже нынешней')
    print('настройки там же. Результат на половине настройки не основание ни')
    print('для чего — он лишь показывает, что имеет смысл нести на проверку.')
    print('=' * 118)
    labels = list(halves)
    head = f'{"вариант":<34}' + ''.join(f'{lab + " · проверка":>32}' for lab in labels)
    print(head)
    print('-' * len(head))
    base_name = names[0]
    for name in names:
        cells, ok = '', []
        for label in labels:
            res = outcome[(label, 'проверка')].get(name)
            ref = outcome[(label, 'проверка')].get(base_name)
            if not res or not ref:
                cells += f'{"—":>32}'
                ok.append(False)
                continue
            lo, _hi = ci(res['r'])
            ok.append(res['mean'] > 0 and res['mean'] >= ref['mean'])
            cell = (f'{res["mean"]:+.3f} [{lo:+.3f}] '
                    f'{"лучше" if res["mean"] >= ref["mean"] else "хуже"}')
            cells += f'{cell:>32}'
        mark = '' if name == base_name else ('  ПРИНЯТ' if ok and all(ok) else '')
        print(f'{name:<34}{cells}{mark}')

    print()
    print('КАК ЧИТАТЬ. Вариант, хороший на настройке и плохой на проверке, —')
    print('это подгонка, и никакие рассуждения этого не отменяют. Вариант,')
    print('хороший на обеих половинах и обоих периодах, пережил четыре')
    print('независимых испытания подряд — это лучшее, что мы вообще умеем')
    print('проверить имеющимися данными.')


if __name__ == '__main__':
    main()
