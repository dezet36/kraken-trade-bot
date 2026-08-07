"""
Уровни: считать ли касания, стоящие вплотную, за одно.

ОТКУДА ВОПРОС. Он не из головы и не из литературы. Пользователь посмотрел на
график сделки, где касания уровня нарисованы кружками, и сказал: «там вообще
пять свечей в ряд, и это считается уровнем». Проверка на данных подтвердила
буквально — на 3614 уровнях бычьего периода:

    касаний ближе 5 баров друг к другу       11%
    касаний ближе 10 баров                   24%
    уровней, где половина касаний — кучка    16%

Причина механическая. Пивот требует всего двух баров с каждой стороны, и в
узком пятачке их набирается несколько подряд. «Касаний 3» получается там, где
цена подходила к уровню ОДИН раз, — а число касаний это и есть вес уровня, по
нему проходит порог MIN_TOUCHES.

Стоит отметить: увидеть это стало возможно только после того, как касания
начали рисовать на графике. До этого в подписи стояло одно число, и проверить
его было нечем.

ЧТО МЕРЯЕТСЯ. Минимальное расстояние между зачтёнными касаниями. Ноль —
нынешнее поведение. Остальные значения режут «кучки» до одного касания, и
уровни, державшиеся на них, перестают проходить порог.

ЭТО ФИЛЬТР, А НЕ ПОСЛАБЛЕНИЕ. Он УБИРАЕТ сетапы, поэтому оправдать себя
обязан качеством: замер уровней на полном пуле уже показал, что все шесть
существующих порогов стоят в оптимуме, и просто «меньше сделок» здесь
улучшением не будет.

ПРИЁМКА. Та же, что в research/quantity.py после исправления: на ОБОИХ
периодах сумма R не упала И отношение суммы R к просадке выросло. Второе —
главное: если фильтр режет мусор, отдача на единицу риска обязана подняться,
даже если денег станет чуть меньше.

Запуск:
    python research/levels_spacing.py
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

PAIRS_LIMIT = 20
# Ноль первым: это нынешнее поведение, с ним и сравниваем.
GAPS = (0, 5, 10, 20, 40)


def orders_for(data, pairs):
    """Заявки уровней при текущем значении MIN_TOUCH_GAP."""
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
        # Уровни строятся ВНУТРИ варианта: разнесённость меняет их состав, а
        # не только отбор сделок. Построй мы их один раз снаружи — замер
        # сравнивал бы вариант сам с собой и этого бы не заметил.
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
                entry_type='stop', meta={'touches': setup['touches']}))
    return out


def run(orders, exec_data):
    from levels import params as P
    from smc_engine import compute_stats, run_portfolio

    if len(orders) < 5:
        return None
    result = run_portfolio(
        orders, exec_data,
        risk_pct=P.RISK_PCT, max_positions=P.MAX_POSITIONS,
        cooldown_hours=P.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False, max_hold_hours=P.MAX_HOLD_HOURS)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 5:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    # Заявка — объект Order, meta у неё полем, а не по ключу. Ту же ошибку я
    # сегодня уже сделал в замере глубины входа: там она обрушила прогон на
    # последней строке, после часа счёта. Здесь — на первом же варианте.
    touches = np.array([(o.meta or {}).get('touches', 0) for o in orders],
                       dtype=float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'mean': float(r.mean()), 'total': float(r.sum()),
            'wr': float((r > 0).mean() * 100), 'dd': stats['max_dd_pct'],
            'touches': float(np.mean(touches)) if len(touches) else 0.0}


def main():
    from levels import params as P

    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        os.environ['SMC_CACHE_DIR'] = cache
        sys.modules.pop('backtest_smc', None)
        import backtest_smc as bt

        print(f'[{label}] загрузка...', flush=True)
        data = {}
        for pair in pairs[:PAIRS_LIMIT]:
            loaded = bt.load_pair(pair)
            if loaded is not None:
                data[pair] = loaded
        periods[label] = (data, list(data), {p: data[p]['1h'] for p in data})

    results = {}
    for label, (data, pairs, exec_data) in periods.items():
        print()
        print('=' * 104)
        print(f'{label}   пар: {len(pairs)}')
        print('=' * 104)
        head = (f'{"разрыв":<10}{"заявок":>9}{"сделок":>8}{"касаний":>9}'
                f'{"винрейт":>9}{"R/сделку":>10}{"сумма R":>9}{"DD%":>7}'
                f'{"R/просадку":>12}{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for gap in GAPS:
            saved = P.MIN_TOUCH_GAP
            P.MIN_TOUCH_GAP = gap
            try:
                res = run(orders_for(data, pairs), exec_data)
            finally:
                P.MIN_TOUCH_GAP = saved
            if res is None:
                print(f'{gap:<10}{"— мало сделок":>18}')
                continue
            results[label][gap] = res
            lo, hi = ci(res['r'])
            eff = res['total'] / res['dd'] if res['dd'] else float('inf')
            tag = f'{gap}' + (' (сейчас)' if gap == 0 else '')
            print(f'{tag:<10}{res["orders"]:>9}{res["n"]:>8}{res["touches"]:>9.1f}'
                  f'{res["wr"]:>8.1f}%{res["mean"]:>10.3f}{res["total"]:>9.1f}'
                  f'{res["dd"]:>7.1f}{eff:>12.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 104)
    print('ПРИЁМКА: на ОБОИХ периодах сумма R не упала И отношение суммы R')
    print('к просадке выросло. Фильтр УБИРАЕТ сделки, поэтому обязан')
    print('оправдаться качеством: просто «меньше сделок» улучшением не будет.')
    print('=' * 104)
    for gap in GAPS:
        if gap == 0:
            continue
        ok, lines = True, []
        for label, table in results.items():
            res, ref = table.get(gap), table.get(0)
            if not res or not ref:
                ok = False
                continue
            eff = res['total'] / res['dd'] if res['dd'] else float('inf')
            ref_eff = ref['total'] / ref['dd'] if ref['dd'] else float('inf')
            lo, hi = diff_ci(res['r'], ref['r'])
            lines.append(f'    {label}: R {res["total"]:+.0f} против '
                         f'{ref["total"]:+.0f}, R/просадку {eff:.1f} против '
                         f'{ref_eff:.1f}, край {res["mean"] - ref["mean"]:+.3f} '
                         f'[{lo:+.3f}; {hi:+.3f}]')
            if not (res['total'] >= ref['total'] and eff > ref_eff):
                ok = False
        print(f'разрыв {gap}' + ('  ПРИНЯТ' if ok and lines else ''))
        for line in lines:
            print(line)


if __name__ == '__main__':
    main()
