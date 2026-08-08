"""
Три работающие стратегии на РАСТУЩИХ периодах — то, что не проверялось ни разу.

ОТКУДА ЭТОТ ЗАМЕР. Проект всё время считал, что проверен на двух режимах: рост
2025-26 и падение 2022-23. Измерение показало, что это неверно:

    период                        BTC        медиана пула
    2022-01 .. 2023-07 «медведь»  −34.7%       −60.2%
    2025-05 .. 2026-07 «бык»      −39.8%       −48.6%

«Бычий» период вырос до 126 150 и упал до 57 756. Оба проверочных периода —
ПАДАЮЩИЕ, и ярлык, стоявший в проекте с самого начала, вводил в заблуждение.

Отсюда два следствия, и оба неприятные.

1. Двусторонняя приёмка спасала от случайных находок, но независимость там была
   только ПО ВРЕМЕНИ, не по режиму. Всё принятое могло оказаться отобранным под
   падающий рынок.
2. «Шорты сильнее лонгов» — не свойство рынка, как докладывалось, а свойство
   двух периодов, оба из которых падали. Проверка нормировки импульса была
   верной (наш код тут ни при чём), но объяснение проще: рынок падал.

Докачанные периоды дают то, чего у проекта не было НИКОГДА: BTC +106.0% и
+49.6%. Здесь три стратегии, на которых уже идёт бумажная торговля, впервые
проверяются в растущем рынке.

ЧТО СЧИТАЕТСЯ ОТВЕТОМ, ЗАПИСАНО ДО ПРОГОНА. Речь не о принятии новой идеи, а о
проверке уже работающей, поэтому и требование другое:

    стратегия считается проверенной по режиму, если на растущих периодах она
    НЕ отрицательна — то есть край не был отобран под падение.

Отрицательный результат на растущем рынке при положительном на падающем означал
бы, что стратегия торгует не край, а направление рынка, и её результат на
бумаге ничего не говорит о будущем.

Отдельно смотрится разбивка по сторонам: если на растущих периодах ЛОНГИ
оказываются сильнее, это подтвердит, что перекос был свойством режима, а не
стратегии, и снимет вопрос о выключении лонгов окончательно.

Запуск:
    python research/rising_check.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import direction_across as da  # noqa: E402
from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from common import ci, hush, unhush  # noqa: E402
from smc_market_regime import load_period  # noqa: E402

MID_PAIRS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT',
    'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT', 'ARBUSDT', 'DOTUSDT',
    'XLMUSDT', 'NEARUSDT', 'UNIUSDT', 'AAVEUSDT', 'COTIUSDT', 'BICOUSDT',
    'SHIB1000USDT',
]

PERIODS = [
    ('2022-01 · падение', BEAR_CACHE, BEAR_PAIRS, False),
    ('2023-07 · РОСТ',    os.path.join(ROOT, 'research',
                                       'backtest_cache_mid1'), MID_PAIRS, True),
    ('2024-07 · РОСТ',    os.path.join(ROOT, 'research',
                                       'backtest_cache_mid2'), MID_PAIRS, True),
    ('2025-05 · падение', BULL_CACHE, BULL_PAIRS, False),
]

BOTH = ('LONG', 'SHORT')


def books(label, data, smc):
    """Заявки трёх стратегий — каждой её собственным боевым путём."""
    import config
    from levels import params as LP
    from smc import params as SP

    pairs = list(data)
    # Исполнение у всех трёх — пятиминутное. Часовое исполнение завышает
    # стоп-заявки примерно на 40%: на грубой свече заявка по стопу не может
    # исполниться хуже цены срабатывания, и у стратегии уровней это давало
    # прибавку из ничего. Часовой словарь здесь не заводится СОЗНАТЕЛЬНО,
    # чтобы его нельзя было подставить по невнимательности.
    exec_5m = {p: data[p]['5m'] for p in pairs}

    quiet = hush()
    try:
        print(f'[{label}] сбор заявок...', flush=True)
        return {
            'FIBO': (da.orders_fibo(data, pairs), exec_5m,
                     dict(risk_pct=config.RISK_PER_TRADE, max_positions=5,
                          cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
                          max_same=getattr(config, 'MAX_SAME_DIRECTION', 0))),
            'LEVELS': (da.orders_levels(data, pairs), exec_5m,
                       dict(risk_pct=LP.RISK_PCT,
                            max_positions=LP.MAX_POSITIONS,
                            cooldown_hours=LP.COOLDOWN_HOURS,
                            max_same=LP.MAX_SAME_DIRECTION,
                            max_hold=LP.MAX_HOLD_HOURS)),
            'SMC': (da.orders_smc(smc), exec_5m,
                    dict(risk_pct=smc['bt'].RISK_PCT,
                         max_positions=smc['bt'].MAX_POSITIONS,
                         cooldown_hours=smc['bt'].COOLDOWN_HOURS,
                         max_same=SP.MAX_SAME_DIRECTION)),
        }
    finally:
        unhush(quiet)


def main():
    results, sides = {}, {}
    for label, cache, pairs, rising in PERIODS:
        data = da.load(cache, pairs, label)
        if not data:
            print(f'   {label}: данных нет')
            continue
        smc = load_period(cache, list(data)[:da.PAIRS_LIMIT], label + ' · smc')
        table = books(label, data, smc)

        print()
        print('=' * 108)
        print(f'{label}   пар: {len(data)}')
        print('=' * 108)
        head = (f'{"стратегия":<10}{"заявок":>8}{"сделок":>8}{"винрейт":>9}'
                f'{"R/сделку":>10}{"сумма R":>10}{"DD%":>7}{"интервал":>22}'
                f'{"лонги":>9}{"шорты":>9}')
        print(head)
        print('-' * len(head))
        for name, (orders, exec_data, kwargs) in table.items():
            res = da.run_side(orders, exec_data, BOTH, **kwargs)
            longs = da.run_side(orders, exec_data, ('LONG',), **kwargs)
            shorts = da.run_side(orders, exec_data, ('SHORT',), **kwargs)
            results[(name, label)] = res
            sides[(name, label)] = (longs, shorts)
            if res is None:
                print(f'{name:<10}{"— мало сделок":>16}')
                continue
            lo, hi = ci(res['r'])
            l_txt = f'{longs["mean"]:+.3f}' if longs else '—'
            s_txt = f'{shorts["mean"]:+.3f}' if shorts else '—'
            print(f'{name:<10}{res["orders"]:>8}{res["n"]:>8}{res["wr"]:>8.1f}%'
                  f'{res["mean"]:>10.3f}{res["total"]:>10.1f}{res["dd"]:>7.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}{l_txt:>9}{s_txt:>9}')

    labels = [label for label, _c, _p, _r in PERIODS]
    rising = [label for label, _c, _p, r in PERIODS if r]

    print()
    print('=' * 108)
    print('ПРОВЕРКА, ЗАПИСАННАЯ ДО ПРОГОНА: край не отобран под падение, если')
    print('на РАСТУЩИХ периодах стратегия не отрицательна.')
    print('=' * 108)
    head = f'{"стратегия":<10}' + ''.join(f'{lab:>24}' for lab in labels)
    print(head)
    print('-' * len(head))
    for name in ('FIBO', 'LEVELS', 'SMC'):
        cells, ok = '', []
        for label in labels:
            res = results.get((name, label))
            if res is None:
                cells += f'{"—":>24}'
                if label in rising:
                    ok.append(False)
                continue
            lo, _hi = ci(res['r'])
            if label in rising:
                ok.append(res['mean'] >= 0)
            # Ячейка собирается ОТДЕЛЬНОЙ строкой: вложенные f-строки с
            # одинарными кавычками внутри одинарных — синтаксическая ошибка до
            # Python 3.12, и она уже роняла один замер после часа счёта.
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}]'
            cells += f'{cell:>24}'
        verdict = 'край держится в росте' if ok and all(ok) else \
                  'В РОСТЕ ОТРИЦАТЕЛЕН — край был под падение'
        print(f'{name:<10}{cells}')
        print(f'{"":<10}{verdict}')

    print()
    print('=' * 108)
    print('ЛОНГИ ПРОТИВ ШОРТОВ ПО РЕЖИМАМ')
    print('Если в росте лонги сильнее, перекос был свойством режима, а не')
    print('стратегии, и вопрос о выключении лонгов снимается.')
    print('=' * 108)
    head = f'{"стратегия":<10}' + ''.join(f'{lab:>24}' for lab in labels)
    print(head)
    print('-' * len(head))
    for name in ('FIBO', 'LEVELS', 'SMC'):
        cells = ''
        for label in labels:
            pair = sides.get((name, label))
            if not pair or not pair[0] or not pair[1]:
                cells += f'{"—":>24}'
                continue
            longs, shorts = pair
            winner = 'лонги' if longs['mean'] > shorts['mean'] else 'шорты'
            cell = f'{longs["mean"]:+.2f}/{shorts["mean"]:+.2f} {winner}'
            cells += f'{cell:>24}'
        print(f'{name:<10}{cells}')


if __name__ == '__main__':
    main()
