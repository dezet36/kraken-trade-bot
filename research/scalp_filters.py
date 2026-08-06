"""
Пробой уровня: последняя непроверенная ось — ОТБОР сетапов.

ЧТО УЖЕ ЗАКРЫТО И ПОЧЕМУ ЭТО НЕ ПОВТОР. Два замера закрыли ГЕОМЕТРИЮ: где
ставить стоп, какая цель, как входить, и то же самое с обратной стороны.
Девять вариантов, два периода, обе стороны — всё в минусе. Но во всех девяти
в торговлю шли ВСЕ 24 564 сетапа одинаково. Отбор не проверялся ни разу.

ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ. Четыре признака, три из них взяты из внешних
источников и один из здравого смысла:

    сохнет объём      объём внутри прижатия ниже, чем до него. Источники
                      дают на этом самое сильное число темы: около 65%
                      попаданий против 48% при объёме ниже среднего;
    сильный всплеск   объём пробойной свечи вдвое выше объёма прижатия —
                      вторая половина того же правила;
    направленность    прижатие одностороннее: растущие минимумы в плоское
                      сопротивление либо падающие максимумы в плоскую
                      поддержку. Поглощение, а не затишье;
    зрелый уровень    три касания и более, возраст выше медианного.

ВСТРОЕННЫЙ КОНТРОЛЬ, БЕЗ КОТОРОГО ЧИТАТЬ ТАБЛИЦУ НЕЛЬЗЯ. Фильтр, который
действительно находит НАСТОЯЩИЕ пробои, обязан двигать две стороны в РАЗНЫЕ
стороны: пробойную улучшать, а сделку против пробоя — ухудшать. Если он
улучшает обе, значит он не отличает настоящий пробой от ложного, а просто
выбирает сетапы с более дешёвой геометрией — и «улучшение» окажется
пересортировкой издержек.

Ровно такой контроль сегодня уже спас от ложного вывода по структуре рынка:
там «по структуре» была в плюсе на обоих периодах, а «против структуры» —
тоже в плюсе на одном из них, и гипотеза отпала.

ПРИЁМКА, ЗАПИСАННАЯ ДО ЗАПУСКА:
    1. вариант в плюсе на ОБОИХ периодах и интервал не накрывает ноль;
    2. контроль ушёл в противоположную сторону;
    3. сделок осталось не меньше сотни на период — иначе это не стратегия.

ЧЕСТНЫЙ АПРИОРИ. Валовый край сейчас -0.106 (пробой) и -0.039 (против).
Чтобы перекрыть издержки в 0.19 R, фильтру нужно сдвинуть край на 0.23-0.30 R.
Фильтры, режущие 80% сетапов, такие величины двигать умеют, но ставлю я на
неудачу. Четыре признака на одних данных — это уже область, где находят шум,
поэтому вариантов ровно четыре, комбинация одна, и правило записано выше.

РЕЗУЛЬТАТ, 2026-08-06. ОТРИЦАТЕЛЬНЫЙ ПО ВСЕМ ЧЕТЫРЁМ. Сдвиг края от каждого
фильтра, пробойная сторона, два периода:

    сохнет объём в прижатии    +0.005   -0.008
    всплеск x2 от прижатия     -0.009   -0.011
    прижатие направленное      +0.006   +0.013
    зрелый уровень             +0.003        —
    всё вместе                 -0.082   -0.039

Нужен был сдвиг 0.23-0.30 R. Получены сотые доли, и знак у трёх из четырёх
меняется между периодами — это шум, а не слабый сигнал.

САМОЕ ВАЖНОЕ ЗДЕСЬ — ПЕРВАЯ СТРОКА. Высыхание объёма было сильнейшим числом
всей темы во внешних источниках: 65% попаданий против 48%. На наших данных
оно двигает результат на +0.005 и -0.008, то есть НЕ ВОСПРОИЗВОДИТСЯ вовсе.
Это стоит помнить в следующий раз, когда чужая статистика будет выглядеть
убедительной: она проверяема, и проверять надо.

Комбинация всех трёх признаков хуже любого по отдельности на обоих периодах
(-0.082 и -0.039). Так ведёт себя не отбор, а подгонка: каждое условие режет
выборку, ни одно не несёт сигнала, и остаток становится всё более случайным.

ЕДИНСТВЕННОЕ, ЧТО ЗАСЛУЖИВАЕТ УПОМИНАНИЯ И НЕ ЗАСЛУЖИВАЕТ ДЕЙСТВИЯ. Сделка
ПРОТИВ пробоя при направленном прижатии дала положительный ВАЛОВЫЙ край на
обоих периодах: +0.049 и +0.105. После издержек это -0.118 и -0.059, то есть
минус. Сделок 291 и 349, интервалы накрывают ноль. Направление любопытное,
но действовать по нему нельзя: это контрольная сторона, а не проверяемая, и
она отобрана тем же перебором, который остальное не подтвердил.

ТЕМА ПРОБОЯ ЗАКРЫТА ПО ВСЕМ ТРЁМ ОСЯМ: геометрия, направление сделки, отбор
сетапов. Три замера, обе стороны, два независимых периода, 24 564 сетапа.
Возвращаться не к чему.

Запуск:
    python research/scalp_filters.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402
from scalp_breakout import PAIRS_LIMIT, build_orders as breakout_orders  # noqa: E402
from scalp_breakout import collect_setups  # noqa: E402
from scalp_fade import build_orders as fade_orders  # noqa: E402

# Геометрия берётся ЛУЧШАЯ из проигравших: стоп за коробку, цель в две высоты,
# вход на возврате. Она потеряла меньше всех (-0.220 и -0.206), и если отбор
# способен что-то спасти, спасать он будет её.
BEST_BREAK = dict(stop_mode='box', target_mult=2.0, entry_mode='retest')
BEST_FADE = dict(mode='return', target_mode='box')


def _median(setups, key):
    values = [s[key] for s in setups
              if s.get(key) is not None and np.isfinite(s.get(key, np.nan))]
    return float(np.median(values)) if values else 0.0


def make_filters(setups):
    """Правила отбора. Пороги — из распределения самих данных, не с потолка."""
    age_median = _median(setups, 'age')
    return [
        ('без отбора (как мерили)', lambda s: True),
        ('сохнет объём в прижатии', lambda s: s.get('vol_dry', 9) < 1.0),
        ('всплеск ×2 от прижатия', lambda s: s.get('vol_surge', 0) >= 2.0),
        ('прижатие направленное', lambda s: bool(s.get('directed'))),
        (f'зрелый уровень (3+, старше {age_median:.0f} баров)',
         lambda s: s.get('touches', 0) >= 3 and s.get('age', 0) >= age_median),
        ('всё вместе',
         lambda s: (s.get('vol_dry', 9) < 1.0 and s.get('vol_surge', 0) >= 2.0
                    and bool(s.get('directed')))),
    ]


def run_side(setups, frames, data, side, keep):
    from scalp import params
    from smc_engine import compute_stats, run_portfolio

    chosen = [s for s in setups if keep(s)]
    if len(chosen) < 5:
        return None
    if side == 'break':
        orders = breakout_orders(chosen, **BEST_BREAK, min_rr=1.5)
    else:
        orders = fade_orders(chosen, frames, **BEST_FADE)
    if len(orders) < 3:
        return None

    result = run_portfolio(
        orders, data,
        risk_pct=params.RISK_PCT, max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * 5 / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 3:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    return {'r': r, 'n': len(trades), 'setups': len(chosen),
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct']}


def load(cache_dir, pairs, label):
    import pandas as pd

    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data, frames, setups = {}, {}, []
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or '5m' not in loaded:
            continue
        df = loaded['5m']
        data[pair] = df
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        frames[pair] = {'_high': df['high'].to_numpy(float),
                        '_low': df['low'].to_numpy(float),
                        '_close': df['close'].to_numpy(float),
                        '_time': stamps.to_numpy()}
        found = collect_setups(pair, df)
        setups += found
        print(f'      {pair}: сетапов {len(found)} (всего {len(setups)})', flush=True)
    return data, frames, setups


def describe(setups, label):
    print()
    print('=' * 108)
    print(f'РАСПРЕДЕЛЕНИЕ ПРИЗНАКОВ · {label}   сетапов {len(setups)}')
    print('=' * 108)
    for key, name in (('vol_dry', 'объём в прижатии / до него'),
                      ('vol_surge', 'всплеск на пробое / прижатие'),
                      ('slope_atr', 'наклон границы, ATR за бар'),
                      ('age', 'возраст уровня, баров'),
                      ('touches', 'касаний')):
        values = np.array([s.get(key, np.nan) for s in setups], dtype=float)
        values = values[np.isfinite(values)]
        if not len(values):
            continue
        q = np.percentile(values, [5, 25, 50, 75, 95])
        print(f'{name:<34}  5% {q[0]:>8.2f}   25% {q[1]:>8.2f}   '
              f'50% {q[2]:>8.2f}   75% {q[3]:>8.2f}   95% {q[4]:>8.2f}')
    directed = sum(1 for s in setups if s.get('directed'))
    print(f'{"прижатие направленное":<34}  {directed} из {len(setups)} '
          f'({directed / max(len(setups), 1) * 100:.0f}%)')


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, frames, setups) in periods.items():
        describe(setups, label)
        print()
        print('=' * 108)
        print(f'{label}')
        print('=' * 108)
        head = (f'{"отбор":<38}{"сторона":<10}{"сетапов":>9}{"сделок":>8}'
                f'{"винрейт":>9}{"R вал.":>9}{"R/сделку":>10}{"сумма R":>9}'
                f'{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, keep in make_filters(setups):
            row = {}
            for side, side_name in (('break', 'пробой'), ('fade', 'против')):
                res = run_side(setups, frames, data, side, keep)
                row[side] = res
                if res is None:
                    print(f'{name:<38}{side_name:<10}{"— мало сделок":>18}')
                    continue
                lo, hi = ci(res['r'])
                print(f'{name:<38}{side_name:<10}{res["setups"]:>9}{res["n"]:>8}'
                      f'{res["wr"]:>8.1f}%{res["gross"]:>9.3f}{res["mean"]:>10.3f}'
                      f'{res["total"]:>9.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>22}')
            results[label][name] = row
            print('-' * len(head))

    print()
    print('=' * 108)
    print('ПРИЁМКА: в плюсе на обоих периодах, интервал не накрывает ноль,')
    print('         контроль («против пробоя») ушёл в ДРУГУЮ сторону')
    print('=' * 108)
    names = [name for name, _ in make_filters(periods[next(iter(periods))][2])]
    for name in names:
        if name.startswith('без отбора'):
            continue
        cells, ok = '', []
        for label, table in results.items():
            row = table.get(name) or {}
            res, ctrl = row.get('break'), row.get('fade')
            base = (table.get('без отбора (как мерили)') or {}).get('break')
            if not res or not base:
                cells += f'{"—":>34}'
                ok.append(False)
                continue
            lo, hi = ci(res['r'])
            moved = res['mean'] - base['mean']
            control_ok = ctrl is not None and ctrl['mean'] < base['mean']
            ok.append(res['mean'] > 0 and lo > 0 and control_ok and res['n'] >= 100)
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}; {hi:+.3f}] Δ{moved:+.3f}'
            cells += f'{cell:>34}'
        mark = '  ПРИНЯТ' if all(ok) and ok else ''
        print(f'{name:<38}{cells}{mark}')

    print()
    print('Если не принят ни один — тема пробоя закрыта по всем трём осям:')
    print('геометрия, направление сделки и отбор сетапов. Возвращаться не к чему.')


if __name__ == '__main__':
    main()
