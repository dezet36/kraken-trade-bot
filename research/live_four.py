"""
FIBO, LEVELS и SMC на четырёх периодах и ОТЛОЖЕННЫХ парах.

ЗАЧЕМ, ЕСЛИ ЕСТЬ rising_check. Тот замер отвечает на свой вопрос, записанный
до прогона: не отрицательны ли работающие стратегии на растущем рынке, то есть
не был ли их край отобран под падение. Вопрос здесь другой и строже:
выдерживают ли они планку, до которой проект дорос уже после их принятия.

Планку подняли два открытия, оба сделанные измерением, а не вкусом.

1. Оба исходных периода оказались падающими: у «бычьего 2025-26» BTC −39.8%, у
   «медвежьего» −34.7%. Приёмка «на двух независимых периодах» была приёмкой на
   двух падающих.
2. Вариант, отобранный на полном пуле пар, развалился на парах, которых отбор
   не видел. Так закрылся пробой канала: «плюс на всех четырёх» держался
   половиной настройки, а отложенная дала ноль и минус.

FIBO, LEVELS и SMC принимались до обоих. То же самое уже проделано для RSIBB
(rsibb_four.py), где выяснилось, что стратегия сидит ровно на пороге
различимости; эти три ни разу так не смотрели.

НИ ОДНОГО ВАРИАНТА. Считается ровно то, что стоит в боевых параметрах каждой
стратегии, и ничего кроме. Перебор превратил бы проверку в новый подбор, а его
результат — в то же, чем оказался «пробой глубже 0.30 ATR»: лучшую из девяти
монеток.

ПАРЫ ДЕЛЯТСЯ ЧЕРЕЗ ОДНУ. Список отсортирован по обороту; деление подряд отдало
бы одной половине крупные инструменты, а другой мелкие, и провал означал бы «на
мелких не работает», а не «подгонка под пул».

ИСПОЛНЕНИЕ ПЯТИМИНУТНОЕ У ВСЕХ. Часовое завышает стратегии со стоп-входом
примерно на 40%: на грубой свече заявка по стопу не может исполниться хуже цены
срабатывания. У стратегии уровней это давало прибавку из ничего.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА. На ОТЛОЖЕННОЙ половине, для КАЖДОЙ стратегии
по отдельности:

    в плюсе на всех четырёх периодах, интервал не накрывает ноль хотя бы на
    одном И не меньше 30 сделок на период.

Ровно та же формулировка, что применялась к RSIBB, — иначе сравнивать их между
собой было бы нельзя. Требование по интервалу здесь один период, а не два:
половина пула даёт половину сделок, и интервалы шире примерно в полтора раза.

ЧТО ЗНАЧИТ ПРОВАЛ, СКАЗАНО ЗАРАНЕЕ. Это НЕ команда выключать стратегию: все три
торгуют бумагой, и там неподтверждённой стратегии место. Провал означает ровно
одно — на настоящие деньги она на этих данных не идёт. Последствие записано так
намеренно: у RSIBB заранее записанное «вывод из бумаги» оказалось неудачным,
потому что уничтожало бы единственный источник недостающих сделок.

Запуск:
    python research/live_four.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import direction_across as da  # noqa: E402
from common import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS,  # noqa: E402
                    RISING_CACHES, RISING_PAIRS, ci, hush, unhush)
from smc_market_regime import load_period  # noqa: E402

BOTH = ('LONG', 'SHORT')
MIN_TRADES = 30
NAMES = ('FIBO', 'LEVELS', 'SMC')

PERIODS = [
    ('2022-01 падение', BEAR_CACHE, BEAR_PAIRS),
    ('2023-07 РОСТ',    RISING_CACHES[0], RISING_PAIRS),
    ('2024-07 РОСТ',    RISING_CACHES[1], RISING_PAIRS),
    ('2025-05 падение', BULL_CACHE, BULL_PAIRS),
]


def books(data, smc):
    """Заявки трёх стратегий с их собственными боевыми настройками."""
    import config
    from levels import params as LP
    from smc import params as SP

    pairs = list(data)
    # Пятиминутное исполнение у всех трёх — см. заголовок.
    exec_5m = {p: data[p]['5m'] for p in pairs}
    quiet = hush()
    try:
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
    results = {}
    for label, cache, pairs in PERIODS:
        usable = pairs[:da.PAIRS_LIMIT]
        # Режим рынка SMC считает по дневным биткойна. Биткойн попадает ровно
        # в одну половину, и брать режим из торгуемого набора нельзя: вторая
        # половина осталась бы без него. Подсунуть биткойн в обе половины тоже
        # нельзя — одна пара протекла бы через отложенную. Поэтому дневные
        # грузятся ОТДЕЛЬНО и в торговлю не идут.
        regime_bars = da.load(cache, ['BTCUSDT'], f'{label} · режим')
        regime_bars = (regime_bars.get('BTCUSDT') or {}).get('1d')
        for part, subset in (('настройка', usable[0::2]),
                             ('проверка', usable[1::2])):
            tag = f'{label} · {part}'
            data = da.load(cache, subset, tag)
            if not data:
                print(f'   {tag}: данных нет')
                continue
            smc = load_period(cache, list(data), tag + ' · smc',
                              regime_bars=regime_bars)
            for name, (orders, exec_data, kwargs) in books(data, smc).items():
                results[(name, label, part)] = da.run_side(
                    orders, exec_data, BOTH, **kwargs)

    for part in ('настройка', 'проверка'):
        print()
        print('=' * 100)
        print('ОТЛОЖЕННАЯ ПОЛОВИНА — по ней и только по ней идёт приёмка'
              if part == 'проверка' else
              'ПОЛОВИНА НАСТРОЙКИ — для сравнения, в приёмке не участвует')
        print('=' * 100)
        head = f'{"период":<20}' + ''.join(f'{n:>26}' for n in NAMES)
        print(head)
        print('-' * len(head))
        for label, _c, _p in PERIODS:
            cells = ''
            for name in NAMES:
                res = results.get((name, label, part))
                if not res:
                    cells += f'{"— мало сделок":>26}'
                    continue
                lo, hi = ci(res['r'])
                cells += '{:>26}'.format(
                    '{:+.3f} [{:+.3f};{:+.3f}] n={}'.format(
                        res['mean'], lo, hi, res['n']))
            print(f'{label:<20}{cells}')

    print()
    print('=' * 100)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: на ОТЛОЖЕННОЙ половине в плюсе на')
    print('всех четырёх, интервал от нуля хотя бы на одном И не меньше')
    print(f'{MIN_TRADES} сделок на период. Провал означает «не идёт на')
    print('настоящие деньги», а не «выключить из бумаги».')
    print('=' * 100)

    for name in NAMES:
        positive, strong, thin, seen = [], 0, False, 0
        for label, _c, _p in PERIODS:
            res = results.get((name, label, 'проверка'))
            if not res:
                thin = True
                continue
            seen += 1
            lo, _hi = ci(res['r'])
            positive.append(res['mean'] > 0)
            strong += 1 if lo > 0 else 0
            thin = thin or res['n'] < MIN_TRADES
        if thin or seen < len(PERIODS):
            verdict = 'МАЛО СДЕЛОК — замер не состоялся'
        elif all(positive) and strong >= 1:
            verdict = 'ПОДТВЕРЖДЁН по нынешней планке'
        else:
            verdict = (f'НЕ ПОДТВЕРЖДЁН — в плюсе на {sum(positive)} из 4, '
                       f'интервал от нуля на {strong}')
        print(f'  {name:<8}{verdict}')

    print()
    print('ВАЖНОЕ ПРИ ЧТЕНИИ. Отказ «в плюсе на всех четырёх, но интервал нигде')
    print('не отходит от нуля» и отказ «знак поменялся на отложенной половине»')
    print('— это разные вещи. Первое означает нехватку сделок, второе —')
    print('отсутствие края. Различать их обязательно: у RSIBB был первый')
    print('случай, у пробоя канала — второй.')


if __name__ == '__main__':
    main()
