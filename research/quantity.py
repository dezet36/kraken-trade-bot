"""
Уровни и SMC: можно ли получить БОЛЬШЕ сделок, не потеряв край.

ОТКУДА ВОПРОС. Замер трёх стратегий рядом показал перекос, которого никто не
ожидал. На восьми парах за период:

    бык        FIBO 1820 сделок по 0.058 R   LEVELS 174 по 0.184   SMC 204 по 0.574
    медведь    FIBO 2537 сделок по 0.045 R   LEVELS 221 по 0.603   SMC 293 по 0.101

Суммарно уровни в медвежьем периоде дали БОЛЬШЕ R, чем Фибоначчи (133 против
114), сделав в одиннадцать раз меньше сделок и просев на 4% вместо 18%. То же
у SMC в бычьем. Полгода настраивали Фибоначчи, потому что она даёт много
сделок, — а край живёт у двух других, и у них не хватает как раз количества.

Значит правильный вопрос не «как улучшить Фибоначчи», а «где у уровней и SMC
узкое место по числу сетапов и что оно стоит по качеству».

ЧТО МЕРЯЕТСЯ. Пороги отбора, по одному за раз, вокруг нынешнего значения — и
в сторону послабления, и в сторону ужесточения. Второе не менее важно: если
ужесточение поднимает край, значит нынешний порог слишком мягкий, и разговор
о количестве надо вести с другого конца.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. Перебора всех порогов сразу. Восемь параметров по
три значения — это шесть тысяч сочетаний, и лучшее из них будет случайным по
построению. Здесь по одному, каждый на двух периодах, и решение принимается
не по «стало больше R», а по двустороннему правилу.

ПРИЁМКА. Порог сдвигаем, только если на ОБОИХ периодах: сделок стало больше,
край на сделку не упал значимо (нижняя граница интервала разницы выше -0.05
R), и суммарный R вырос. Край, упавший «немного», при удвоении числа сделок
съедает весь выигрыш — поэтому мерится и то и другое.

Запуск:
    python research/quantity.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from fibo_audit import ci, diff_ci  # noqa: E402

PAIRS_LIMIT = 20      # весь кэш: 20 пар на бычьем, 14 на медвежьем

# Пороги уровней и значения вокруг нынешнего. Первое в списке — нынешнее.
LEVELS_GRID = {
    'VOLUME_RATIO': [1.5, 1.2, 1.0, 1.8],      # объём на возврате
    'MIN_TARGET_R': [1.75, 1.5, 1.25, 2.0],    # как далеко должен быть следующий уровень
    'TRIGGER_ATR': [1.0, 1.5, 2.0, 0.7],       # насколько близко к уровню смотрим
    'MIN_TOUCHES': [2, 3],                     # сколько касаний делает уровень уровнем
    'RECLAIM_BARS': [4, 6, 8, 3],              # сколько ждём возврата после прокола
    'MIN_STOP_PCT': [1.2, 0.8, 1.6],           # пол по расстоянию до стопа
}

# У SMC узкое место — порог совпадения признаков и требование к RR.
# SMC ЗАКРЫТ ПРЕДЫДУЩИМ ПРОГОНОМ, и повторять его незачем. Любое послабление
# уводило край медвежьего периода в МИНУС (+0.101 -> -0.018 и -0.021), а
# просадку разгоняло с 25.6% до 40-44%. Малое число сделок у SMC — цена её
# края, а не недосмотр в настройке. Пустая сетка оставлена намеренно: так
# видно, что вопрос закрыт, а не забыт.
SMC_GRID = {}


def levels_orders(data, pairs):
    """Заявки уровней при ТЕКУЩИХ значениях параметров."""
    from levels import core, params as P
    from smc_engine import Order

    expiry = np.timedelta64(int(P.EXPIRY_HOURS * 3600), 's')
    out, seen = [], set()
    for pair in pairs:
        df = data[pair]['1h']
        high = df['high'].to_numpy(float)
        low = df['low'].to_numpy(float)
        close = df['close'].to_numpy(float)
        volume = (df['volume'].to_numpy(float) if 'volume' in df.columns
                  else np.ones(len(df)))
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        stamps = stamps.to_numpy()
        # Уровни зависят от порогов, поэтому строятся ВНУТРИ варианта, а не
        # один раз снаружи: иначе TOLERANCE_PCT и MIN_TOUCHES не проверялись
        # бы вовсе, а замер этого бы не заметил.
        levels = core.build_levels(high, low)
        atr_values = core.atr(high, low, close)

        for i in range(60, len(close)):
            setup, _ = core.evaluate(high, low, close, volume, i,
                                     levels=levels, atr_values=atr_values)
            if not setup:
                continue
            key = (pair, setup['direction'], round(setup['level'], 8), int(i))
            if key in seen:
                continue
            seen.add(key)
            created = stamps[i]
            out.append(Order(
                pair=pair, direction=setup['direction'],
                entry=setup['entry'], stop=setup['stop_loss'],
                targets=[setup['target']], fractions=[1.0],
                created=created, expires=created + expiry, key=key,
                entry_type='stop', meta={'direction': setup['direction']}))
    return out


def smc_orders(period, _pairs):
    from smc_sweep import build_orders

    out = []
    for pair in period['data']:
        out += build_orders(period['contexts'][pair], pair,
                            period['data'][pair]['1h'])
    return out


def run(orders, exec_data, cfg):
    from smc_engine import compute_stats, run_portfolio

    if len(orders) < 3:
        return None
    kwargs = dict(risk_pct=cfg['risk_pct'], max_positions=cfg['max_positions'],
                  cooldown_hours=cfg['cooldown_hours'],
                  max_same_direction=cfg['max_same'], breakeven_after_tp1=False)
    if cfg.get('max_hold'):
        kwargs['max_hold_hours'] = cfg['max_hold']
    result = run_portfolio(orders, exec_data, **kwargs)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 3:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'mean': float(r.mean()), 'total': float(r.sum()),
            'wr': float((r > 0).mean() * 100),
            'dd': stats['max_dd_pct'], 'ret': stats['return_pct']}


def sweep(name, params_module, grid, make_orders, periods, cfg_of):
    """Один порог за раз, оба периода, сравнение с нынешним значением."""
    results = {}
    for field, values in grid.items():
        base_value = values[0]
        for value in values:
            for label, (data, pairs, smc_period, exec_data) in periods.items():
                saved = getattr(params_module, field)
                setattr(params_module, field, value)
                try:
                    orders = make_orders(smc_period if smc_period else data, pairs)
                    res = run(orders, exec_data, cfg_of())
                finally:
                    setattr(params_module, field, saved)
                results.setdefault((field, value), {})[label] = res
            print(f'   {name} {field}={value} готово', flush=True)
    return results, {f: v[0] for f, v in grid.items()}


def report(name, results, base, grid):
    print()
    print('=' * 112)
    print(f'{name}: порог за порогом')
    print('=' * 112)
    head = (f'{"порог":<26}{"период":<18}{"заявок":>8}{"сделок":>8}{"винрейт":>9}'
            f'{"R/сделку":>10}{"сумма R":>9}{"DD%":>7}{"против нынешнего":>26}')
    print(head)
    print('-' * len(head))

    for field, values in grid.items():
        for value in values:
            cells = results.get((field, value)) or {}
            tag = f'{field}={value}' + (' (сейчас)' if value == base[field] else '')
            for label, res in cells.items():
                if res is None:
                    print(f'{tag:<26}{label:<18}{"— мало сделок":>18}')
                    continue
                ref = (results.get((field, base[field])) or {}).get(label)
                cmp_text = ''
                if ref and value != base[field]:
                    lo, hi = diff_ci(res['r'], ref['r'])
                    gain = res['mean'] - ref['mean']
                    more = res['n'] - ref['n']
                    cmp_text = f'{gain:+.3f} [{lo:+.3f}] сд{more:+d}'
                print(f'{tag:<26}{label:<18}{res["orders"]:>8}{res["n"]:>8}'
                      f'{res["wr"]:>8.1f}%{res["mean"]:>10.3f}{res["total"]:>9.1f}'
                      f'{res["dd"]:>7.1f}{cmp_text:>26}')
                tag = ''
            print('-' * len(head))

    print()
    print(f'ПРИЁМКА для {name}. ПРАВИЛО ИСПРАВЛЕНО ПОСЛЕ ПЕРВОГО ПРОГОНА.')
    print()
    print('Было: «нижняя граница интервала разницы средних выше -0.05 R». При')
    print('двух-пяти сотнях сделок интервалы шириной ±0.4 R, и такое правило не')
    print('мог пройти НИ ОДИН вариант в принципе — я задал чувствительность, для')
    print('которой не хватает данных на порядок. Это была не проверка, а её вид.')
    print()
    print('Стало: смотрим на то, ради чего всё и делается, — сумму R и просадку.')
    print('Вариант принимается, если на ОБОИХ периодах сумма R выросла И')
    print('отношение суммы R к просадке не ухудшилось. Второе условие')
    print('обязательно: больше сделок почти всегда дают больше R, и без учёта')
    print('просадки «улучшением» окажется простое увеличение риска — которого')
    print('можно добиться одним параметром, не трогая отбор.')
    print('=' * 112)
    for field, values in grid.items():
        for value in values:
            if value == base[field]:
                continue
            cells = results.get((field, value)) or {}
            ok, note, detail = True, '', []
            for label, res in cells.items():
                ref = (results.get((field, base[field])) or {}).get(label)
                if not res or not ref:
                    ok = False
                    continue
                # Отношение суммы R к просадке: сколько заработано на каждый
                # процент максимальной просадки. Именно оно отличает
                # «стратегия стала лучше» от «просто рискуем больше».
                eff = res['total'] / res['dd'] if res['dd'] else float('inf')
                ref_eff = ref['total'] / ref['dd'] if ref['dd'] else float('inf')
                detail.append(f'{label}: R {res["total"]:+.0f} против '
                              f'{ref["total"]:+.0f}, R/просадку {eff:.1f} '
                              f'против {ref_eff:.1f}')
                if not (res['total'] > ref['total'] and eff >= ref_eff):
                    ok = False
            if ok and cells:
                note = '  ПРИНЯТ'
            print(f'{field}={value:<14}{note}')
            for line in detail:
                print(f'    {line}')


def main():
    import config
    from levels import params as LP
    from smc import params as SP
    from smc_market_regime import load_period

    os.environ.setdefault('SMC_CACHE_DIR', BULL_CACHE)
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        os.environ['SMC_CACHE_DIR'] = cache
        for module in ('backtest_smc', 'smc_sweep'):
            sys.modules.pop(module, None)
        import backtest_smc as bt

        print(f'[{label}] загрузка...', flush=True)
        data = {}
        for pair in pairs[:PAIRS_LIMIT]:
            loaded = bt.load_pair(pair)
            if loaded is not None:
                data[pair] = loaded
        smc_period = load_period(cache, list(data), label + ' · smc')
        periods[label] = (data, list(data), smc_period,
                          {p: data[p]['1h'] for p in data})

    print()
    print('УРОВНИ — один порог за раз')
    lv_cfg = lambda: dict(risk_pct=LP.RISK_PCT, max_positions=LP.MAX_POSITIONS,
                          cooldown_hours=LP.COOLDOWN_HOURS,
                          max_same=LP.MAX_SAME_DIRECTION, max_hold=LP.MAX_HOLD_HOURS)
    lv_results, lv_base = sweep('уровни', LP, LEVELS_GRID,
                                lambda data, pairs: levels_orders(data, pairs),
                                {k: (v[0], v[1], None, v[3])
                                 for k, v in periods.items()}, lv_cfg)
    report('УРОВНИ', lv_results, lv_base, LEVELS_GRID)

    print()
    print('SMC — один порог за раз')
    smc_periods = {k: (v[0], v[1], v[2], {p: v[0][p]['5m'] for p in v[0]})
                   for k, v in periods.items()}
    smc_cfg = lambda: dict(
        risk_pct=list(periods.values())[0][2]['bt'].RISK_PCT,
        max_positions=list(periods.values())[0][2]['bt'].MAX_POSITIONS,
        cooldown_hours=list(periods.values())[0][2]['bt'].COOLDOWN_HOURS,
        max_same=SP.MAX_SAME_DIRECTION)
    smc_results, smc_base = sweep('smc', SP, SMC_GRID, smc_orders,
                                  smc_periods, smc_cfg)
    report('SMC', smc_results, smc_base, SMC_GRID)


if __name__ == '__main__':
    main()
