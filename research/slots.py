"""
Сколько позиций держать одновременно: предел, который никто не перемеривал.

ОТКУДА ВОПРОС — ИЗ НАШЕГО ЖЕ КОДА. В config.py над MAX_ACTIVE_PAIRS стоит
признание:

    v3 2026-07-05: 5 — на пуле из 16 пар cap=5 даёт лучший фронт прибыль/просадка
    2026-08-05: пул вырос до 21 пары, а этот cap заново НЕ подбирался.

Предел подбирался под шестнадцать пар и три месяца назад. С тех пор пул вырос
на треть, стратегий стало три, и каждая ищет сетапы во всём пуле. Предел на
одновременные позиции — это ровно то место, где найденный сетап выбрасывается
не потому, что он плох, а потому что нет свободного слота.

ПОЧЕМУ ЭТО ГЛАВНЫЙ ВОПРОС ИМЕННО СЕЙЧАС. Стоячая цель — больше сделок и лучше
качеством. За два дня закрыто шесть семейств идей (пробой, отбой, возврат к
среднему, сетка, ложный пробой, волны Эллиотта), и ни одно не дало края. При
этом уже измеренное расширение пула дало у стратегии уровней 459 сделок против
174 и 127.9 R против 31.9 при МЕНЬШЕЙ просадке. То есть количество бралось не
из новой стратегии, а из снятия ограничения. Здесь снимается следующее.

ЧТО ЭТО НЕ ЕСТЬ. Это не поиск края: край у трёх стратегий уже принят, и заново
он не проверяется. Это вопрос о РАЗМЕРЕ — сколько сигналов уже принятой
стратегии брать одновременно. Поэтому приёмка построена на эффективности, а не
на значимости разницы средних: требовать, чтобы средний R на сделку значимо не
упал, при выборке в 400 сделок и интервале шириной 0.35 R невозможно в принципе
— такое правило уже писалось однажды и оказалось не проверкой, а её видом.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА. Предел меняется, только если по сравнению с
нынешним на ОБОИХ периодах одновременно:

    1. суммарный R выше;
    2. просадка не больше 25%;
    3. отношение суммарного R к просадке не ниже нынешнего.

Третье условие и есть суть: без него «больше сделок» покупается просто большим
плечом, и рост суммарного R ничего не сообщает. Одновременный риск при пределе
N равен N × 0.5% депозита, и при N = 12 это уже 6%.

Запуск:
    python research/slots.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import direction_across as da  # noqa: E402
from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402
from common import hush, unhush  # noqa: E402
from smc_market_regime import load_period  # noqa: E402

CAPS = (3, 5, 8, 12, 16, 21)
BOTH = ('LONG', 'SHORT')


def books_for(label, data, smc_period):
    """Заявки трёх стратегий. Считаются один раз на период."""
    import config
    from levels import params as LP
    from smc import params as SP

    pairs = list(data)
    exec_1h = {p: data[p]['1h'] for p in pairs}
    exec_5m = {p: data[p]['5m'] for p in pairs}

    quiet = hush()
    try:
        print(f'[{label}] сбор заявок...', flush=True)
        return {
            'FIBO': (da.orders_fibo(data, pairs), exec_5m,
                     dict(risk_pct=config.RISK_PER_TRADE,
                          cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
                          max_same=getattr(config, 'MAX_SAME_DIRECTION', 0)),
                     getattr(config, 'MAX_OPEN_POSITIONS', 5), None),
            'LEVELS': (da.orders_levels(data, pairs), exec_1h,
                       dict(risk_pct=LP.RISK_PCT,
                            cooldown_hours=LP.COOLDOWN_HOURS,
                            max_same=LP.MAX_SAME_DIRECTION),
                       LP.MAX_POSITIONS, LP.MAX_HOLD_HOURS),
            # Параметры портфеля у SMC живут в backtest_smc, а не в smc/params:
            # там их и берёт её собственный замер.
            'SMC': (da.orders_smc(smc_period), exec_5m,
                    dict(risk_pct=smc_period['bt'].RISK_PCT,
                         cooldown_hours=smc_period['bt'].COOLDOWN_HOURS,
                         max_same=SP.MAX_SAME_DIRECTION),
                    smc_period['bt'].MAX_POSITIONS, None),
        }
    finally:
        unhush(quiet)


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        data = da.load(cache, pairs, label)
        smc = load_period(cache, list(data)[:da.PAIRS_LIMIT], label + ' · smc')
        periods[label] = (data, smc)

    results, current = {}, {}
    for label, (data, smc) in periods.items():
        books = books_for(label, data, smc)
        print()
        print('=' * 104)
        print(f'{label}')
        print('=' * 104)
        head = (f'{"стратегия":<10}{"предел":>8}{"заявок":>8}{"сделок":>8}'
                f'{"винрейт":>9}{"R/сделку":>10}{"сумма R":>10}{"доход%":>9}'
                f'{"DD%":>7}{"R/просадку":>12}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, (orders, exec_data, kwargs, now, max_hold) in books.items():
            current[name] = now
            for cap in CAPS:
                res = da.run_side(orders, exec_data, BOTH,
                                  max_positions=cap, max_hold=max_hold, **kwargs)
                if res is None:
                    continue
                res['eff'] = res['total'] / res['dd'] if res['dd'] > 0 else 0.0
                results[label][(name, cap)] = res
                lo, hi = ci(res['r'])
                mark = '  ← нынешний' if cap == now else ''
                print(f'{name:<10}{cap:>8}{res["orders"]:>8}{res["n"]:>8}'
                      f'{res["wr"]:>8.1f}%{res["mean"]:>10.3f}{res["total"]:>10.1f}'
                      f'{res["ret"]:>9.1f}{res["dd"]:>7.1f}{res["eff"]:>12.1f}'
                      f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}{mark}')
            print('-' * len(head))

    print()
    print('=' * 104)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: на ОБОИХ периодах суммарный R выше')
    print('нынешнего, просадка не больше 25% И отношение R к просадке не ниже.')
    print('=' * 104)
    labels = list(results)
    head = f'{"стратегия":<10}{"предел":>8}' + ''.join(
        f'{lab:>34}' for lab in labels)
    print(head)
    print('-' * len(head))
    for name in current:
        now = current[name]
        base = {lab: results[lab].get((name, now)) for lab in labels}
        for cap in CAPS:
            if cap == now:
                continue
            cells, ok = '', []
            for lab in labels:
                res, ref = results[lab].get((name, cap)), base[lab]
                if not res or not ref:
                    cells += f'{"—":>34}'
                    ok.append(False)
                    continue
                ok.append(res['total'] > ref['total'] and res['dd'] <= 25
                          and res['eff'] >= ref['eff'])
                cell = (f'{res["total"]:+.0f}R (было {ref["total"]:+.0f}) '
                        f'DD {res["dd"]:.0f}%')
                cells += f'{cell:>34}'
            print(f'{name:<10}{cap:>8}{cells}'
                  f'{"  ПРИНЯТ" if ok and all(ok) else ""}')
        print('-' * len(head))

    print()
    print('КАК ЧИТАТЬ. «R/просадку» — сколько единиц риска стратегия приносит на')
    print('процент просадки. Если предел растёт, а это число падает, значит')
    print('лишние слоты покупаются плечом, а не находятся в рынке.')


if __name__ == '__main__':
    main()
