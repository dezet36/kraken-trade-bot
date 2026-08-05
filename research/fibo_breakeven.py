"""
Помогает ли фибо перенос стопа в безубыток при пробое уровня B.

ЗАЧЕМ. В коде стратегии уровней написано: «Безубыток выключен: замер
показал, что он режет результат у всех трёх стратегий проекта». У SMC он
тоже выключен и там же объяснено, чем именно мешает. А у фибо
BREAKEVEN_AT_B = True — включён и никогда отдельно не проверялся. Либо та
фраза неверна, либо у фибо работает правило, которое замер отверг.

Правило: как только цена доходит до конца импульса (уровень B, 0%
коррекции), стоп переносится на цену входа. Замысел понятен — движение
пошло в нашу сторону, дальше рисковать нечем. Цена этого замысла тоже
понятна: стоп на входе выбивается любым откатом, а цель у фибо одна и
далеко.

КАК МЕРЯЕТСЯ. Поток сетапов строится ОДИН раз и прогоняется дважды — с
переносом стопа и без, — на двух независимых периодах: бычьем 2025-26 и
медвежьем 2022-23. Один поток на оба прогона важен вдвойне: он вдвое дешевле
(поиск сетапов зовёт боевую analyze_market на каждой часовой свече каждой
пары) и убирает лишний источник разницы — сетапы заведомо одни и те же,
отличается только правило выхода.

Сделки при этом всё равно не парные: перенос стопа меняет, когда
освобождаются слоты, и дальше портфели расходятся. Поэтому сравниваются
распределения R, а не сделка со сделкой.

КРИТЕРИЙ. Выключать безубыток стоит, только если выигрыш виден на ОБОИХ
периодах и доверительный интервал разницы средних не накрывает ноль.
Улучшение на одном периоде — это выбор лучшей половины монетки.

Запуск:
    python research/fibo_breakeven.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS)

BOOTSTRAP = 10_000
RNG = np.random.default_rng(20260805)


def diff_ci(a, b, alpha=0.05):
    """
    Интервал разницы средних двух НЕзависимых выборок.

    Ресемплируем каждую выборку отдельно: сделки в двух прогонах не парные,
    и вычитать их попарно было бы подгонкой под удобный ответ.
    """
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or len(b) < 3:
        return (np.nan, np.nan)
    da = RNG.choice(a, size=(BOOTSTRAP, len(a)), replace=True).mean(axis=1)
    db = RNG.choice(b, size=(BOOTSTRAP, len(b)), replace=True).mean(axis=1)
    d = da - db
    return tuple(np.percentile(d, [alpha / 2 * 100, (1 - alpha / 2) * 100]))


def load(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data = {}
    for pair in pairs:
        loaded = bt.load_pair(pair)
        if loaded is not None:
            data[pair] = loaded
    print(f'   пар: {len(data)}', flush=True)
    return data, bt


def build(data, bt):
    """
    Поток сетапов. Считается один раз на период — это самая дорогая часть
    замера: поиск зовёт боевую analyze_market на каждой часовой свече.
    """
    import config

    config.BREAKEVEN_AT_B = True           # чтобы be_level доехал до заявки
    orders = []
    for pair in data:
        orders += bt.fibo_orders(pair, data[pair])
        print(f'      {pair}: заявок {len(orders)}', flush=True)
    return orders


def run_fibo(data, orders, breakeven):
    """Тот же поток заявок с переносом стопа и без него."""
    import copy

    import config
    from smc_engine import run_portfolio

    # Правило выхода живёт в самой заявке. Копируем поток, чтобы второй
    # прогон не получил заявки, испорченные первым.
    orders = [copy.copy(o) for o in orders]
    if not breakeven:
        for o in orders:
            o.be_trigger = None
    if not orders:
        return pd.DataFrame()

    result = run_portfolio(
        orders, {p: data[p]['5m'] for p in data},
        risk_pct=config.RISK_PER_TRADE,
        max_positions=getattr(config, 'MAX_OPEN_POSITIONS', 5),
        cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
        max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0),
        breakeven_after_tp1=False)         # у фибо одна цель, правило другое

    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        rows.append({'r': t['pnl'] / t['risk'],
                     'reason': str(t.get('exit_reason', '')),
                     'mfe_r': float(t.get('mfe_r', 0) or 0)})
    return pd.DataFrame(rows)


def describe(df):
    if df.empty:
        return dict(n=0, wr=np.nan, mean=np.nan, total=np.nan)
    return dict(n=len(df), wr=(df['r'] > 0).mean() * 100,
                mean=df['r'].mean(), total=df['r'].sum())


def main():
    periods = [('бык 2025-26', BULL_CACHE, BULL_PAIRS),
               ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)]

    verdicts = []
    for label, cache, pairs in periods:
        data, bt = load(cache, pairs, label)
        print(f'   [{label}] ищу сетапы...', flush=True)
        orders = build(data, bt)
        print(f'   [{label}] заявок всего: {len(orders)}', flush=True)

        on = run_fibo(data, orders, breakeven=True)
        off = run_fibo(data, orders, breakeven=False)

        print()
        print('=' * 88)
        print(f'{label}')
        print('=' * 88)
        head = f'{"безубыток":<12}{"сделок":>8}{"винрейт":>9}{"R/сделку":>11}{"сумма R":>10}'
        print(head)
        print('-' * len(head))
        for name, df in (('включён', on), ('выключен', off)):
            s = describe(df)
            print(f'{name:<12}{s["n"]:>8}{s["wr"]:>8.1f}%{s["mean"]:>11.3f}'
                  f'{s["total"]:>10.1f}')

        lo, hi = diff_ci(off['r'], on['r'])
        gain = off['r'].mean() - on['r'].mean()
        crosses = not (lo > 0 or hi < 0)
        print()
        print(f'выключить − включить: {gain:+.3f} R/сделку   '
              f'интервал [{lo:+.3f}; {hi:+.3f}]'
              f'{"  — накрывает ноль" if crosses else ""}')

        # Сколько сделок вообще ТРОНУЛ перенос: если правило почти не
        # срабатывает, спорить не о чем.
        be_exits = on['reason'].str.contains('SL').sum() if not on.empty else 0
        near_zero = ((on['r'] > -0.15) & (on['r'] < 0.05)).sum() if not on.empty else 0
        print(f'из них вышли по стопу: {be_exits}, из них около нуля '
              f'(похоже на безубыток): {near_zero}')

        verdicts.append((label, gain, lo, hi, crosses))

    print()
    print('=' * 88)
    print('ИТОГ')
    print('=' * 88)
    both = all(g > 0 and not c for _, g, _, _, c in verdicts)
    for label, gain, lo, hi, crosses in verdicts:
        mark = 'за выключение' if gain > 0 and not crosses else 'не показано'
        print(f'{label:<18}{gain:+.3f} R  [{lo:+.3f}; {hi:+.3f}]  {mark}')
    print()
    print('Выключать безубыток у фибо:', 'ДА' if both else 'НЕТ — не доказано')


if __name__ == '__main__':
    main()
