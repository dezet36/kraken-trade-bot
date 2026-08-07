"""
Скальпинг на пробое уровня после прижатия: есть ли там край после издержек.

ПОЧЕМУ ЭТОТ ЗАМЕР ВООБЩЕ ДЕЛАЕТСЯ, ЕСЛИ ПРОБОЙ УЖЕ ОТВЕРГНУТ. Прежний замер
(research/breakout.py) мерил ГОЛЫЙ пробой канала и получил доказанный минус:
-0.455 R с интервалом [-0.64; -0.26] в режиме роста. Вывод тогда записали
так: уход цены за уровень систематически не продолжается, за экстремумом
стоят стопы, их собирают, цена возвращается.

Здесь проверяется другое. Добавлены три условия, которых там не было:

    прижатие  цена подошла к уровню и замерла — диапазон последних баров
              в нижней четверти своего распределения;
    закрытие  пробоем считается только ЗАКРЫТИЕ за уровнем, а не прокол
              тенью — тень за уровнем и есть то самое снятие ликвидности;
    объём     на пробойной свече выше среднего.

Если край появится, значит дело было в отборе, а не в самой идее. Если нет —
идея закрыта окончательно, уже на двух разных её формулировках.

ЧТО РЕШАЮТ ИЗДЕРЖКИ. Вход стоп-ордером платит тейкера и проскальзывание с
обеих сторон: круг 0.21% от объёма. При стопе 0.5% это 0.42 R с КАЖДОЙ
сделки. Поэтому меряется и второй способ входа — лимитом на возврате к
пробитому уровню: мейкерская комиссия вместо тейкерской и отсев ложных
пробоев ценой пропущенных импульсов.

ПРИЁМКА. Двусторонняя, как всё в этой работе: вариант принимается, только
если он в плюсе НА ОБОИХ периодах и интервал среднего не накрывает ноль.

Запуск:
    python research/scalp_breakout.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BULL_CACHE, BULL_PAIRS, BEAR_CACHE, BEAR_PAIRS, ci  # noqa: E402

PAIRS_LIMIT = 6          # 5-минутки: 115 тысяч баров на пару, дороже не нужно
BAR_MS = 5 * 60 * 1000


def collect_setups(pair, df):
    """
    Все сетапы пары. Считается один раз: поиск не зависит от того, какой стоп
    и какая цель будут выбраны потом, — а это самая дорогая часть.
    """
    from scalp import core, params

    high = df['high'].to_numpy(float)
    low = df['low'].to_numpy(float)
    close = df['close'].to_numpy(float)
    volume = df['volume'].to_numpy(float)
    stamps = pd.to_datetime(df['timestamp'])
    if getattr(stamps.dt, 'tz', None) is not None:
        stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
    stamps = stamps.to_numpy()

    warmup = max(params.SQUEEZE_BARS, params.VOLUME_WINDOW,
                 params.MAX_AGE_BARS) + params.PIVOT_N + 5
    out = []
    for i in range(warmup, len(close) - 1):
        setup = core.find_setup(high, low, close, volume, i)
        if setup:
            setup['at'] = i
            setup['time'] = stamps[i]
            setup['pair'] = pair
            out.append(setup)
    return out


def build_orders(setups, stop_mode, target_mult, entry_mode, min_rr):
    """Заявки нужной геометрии из готовых сетапов."""
    from scalp import core, params
    from smc_engine import Order

    expiry = np.timedelta64(params.RETEST_MAX_BARS * 5 * 60, 's')
    orders = []
    for s in setups:
        if entry_mode == 'break':
            # Вход по цене закрытия пробойной свечи, стоп-ордером: движок
            # исполнит его на следующем баре и возьмёт тейкера. Заявка живёт
            # один бар — импульсный вход либо случается сразу, либо не нужен.
            entry = s['close']
            life = np.timedelta64(2 * 5 * 60, 's')
            kind = 'stop'
        else:
            # Лимит на возврате к пробитому уровню, чуть не доводя до него:
            # мейкерская комиссия и отсев тех пробоев, что не вернулись.
            offset = params.RETEST_OFFSET_ATR * s['atr']
            entry = (s['level'] + offset if s['direction'] == core.LONG
                     else s['level'] - offset)
            life = expiry
            kind = 'limit'

        trade = core.build_trade(s, entry, stop_mode, target_mult, min_rr)
        if trade is None:
            continue

        created = s['time']
        orders.append(Order(
            pair=s['pair'],
            direction=s['direction'],
            entry=trade['entry'], stop=trade['stop'],
            targets=[trade['target']], fractions=[1.0],
            created=created, expires=created + life,
            key=(s['pair'], s['direction'], round(s['level'], 8), int(s['at'])),
            entry_type=kind,
            meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                  'touches': s['touches'], 'volume_ratio': s['volume_ratio']},
        ))
    return orders


def run_variant(setups, exec_data, stop_mode, target_mult, entry_mode, min_rr):
    from scalp import params
    from smc_engine import compute_stats, run_portfolio

    orders = build_orders(setups, stop_mode, target_mult, entry_mode, min_rr)
    if not orders:
        return None
    result = run_portfolio(
        orders, exec_data,
        risk_pct=params.RISK_PCT, max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * 5 / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if not trades:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    return {'r': r, 'n': len(trades), 'mean': float(r.mean()),
            'total': float(r.sum()), 'winrate': float((r > 0).mean() * 100),
            'costs': float(costs.mean()), 'orders': len(orders),
            'dd': stats['max_dd_pct'], 'ret': stats['return_pct']}


def load(cache_dir, pairs, label):
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    data, setups = {}, []
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or '5m' not in loaded:
            continue
        data[pair] = loaded['5m']
        found = collect_setups(pair, loaded['5m'])
        setups += found
        print(f'      {pair}: сетапов {len(found)} (всего {len(setups)})', flush=True)
    return data, setups


VARIANTS = [
    ('пробой · стоп за коробку · цель x2',   'box',   2.0, 'break'),
    ('пробой · стоп за уровень · цель x2',   'level', 2.0, 'break'),
    ('пробой · стоп за уровень · цель x3',   'level', 3.0, 'break'),
    ('возврат · стоп за коробку · цель x2',  'box',   2.0, 'retest'),
    ('возврат · стоп за уровень · цель x2',  'level', 2.0, 'retest'),
    ('возврат · стоп за уровень · цель x3',  'level', 3.0, 'retest'),
]


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, setups) in periods.items():
        print()
        print('=' * 104)
        print(f'{label}   сетапов: {len(setups)}   пар: {len(data)}')
        print('=' * 104)
        head = (f'{"вариант":<38}{"заявок":>8}{"сделок":>8}{"винрейт":>9}'
                f'{"R/сделку":>10}{"издержки R":>12}{"сумма R":>9}{"интервал среднего":>24}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, stop_mode, mult, entry in VARIANTS:
            res = run_variant(setups, data, stop_mode, mult, entry, 1.5)
            if res is None:
                print(f'{name:<38}{"— сделок нет":>8}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<38}{res["orders"]:>8}{res["n"]:>8}{res["winrate"]:>8.1f}%'
                  f'{res["mean"]:>10.3f}{res["costs"]:>12.3f}{res["total"]:>9.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}')

    print()
    print('=' * 104)
    print('ПРИЁМКА: в плюсе на ОБОИХ периодах и интервал не накрывает ноль')
    print('=' * 104)
    for name, _sm, _tm, _em in VARIANTS:
        verdicts = []
        cells = ''
        for label, table in results.items():
            res = table.get(name)
            if not res:
                cells += f'{"—":>28}'
                verdicts.append(False)
                continue
            lo, hi = ci(res['r'])
            good = res['mean'] > 0 and lo > 0
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}; {hi:+.3f}]'
            cells += f'{cell:>28}'
            verdicts.append(good)
        mark = '  ПРИНЯТ' if all(verdicts) and verdicts else ''
        print(f'{name:<38}{cells}{mark}')

    print()
    print('Если ни один вариант не принят — идея пробоя закрыта окончательно,')
    print('уже на двух разных её формулировках, и повторять её не нужно.')


if __name__ == '__main__':
    main()
