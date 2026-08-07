"""
Фибоначчи: перекос в фильтре скорости, найденный при разборе кода.

ЧТО НАЙДЕНО. Размер импульса считается так:

    size_pct = size / e_price * 100

Нормируется на КОНЕЦ импульса. Для лонга это вершина, для шорта — низ. Одно и
то же движение даёт шорту размер больше: ход со 100 до 105 в лонг это 5/105 =
4.76%, тот же ход обратно в шорт — 5/100 = 5.00%. Разница около 5%, и на неё
шорт легче проходит пороги MIN_IMPULSE_PCT и MIN_IMPULSE_VELOCITY.

ПОЧЕМУ ЭТО НЕ МЕЛОЧЬ, ХОТЯ ВЫГЛЯДИТ МЕЛОЧЬЮ. Перекос НАПРАВЛЕННЫЙ, а именно
перекос лонг/шорт у этой стратегии мы изучали весь день: лонги дают ровно
ноль, шорты работают. Часть этой разницы могла создаваться самим фильтром —
он пропускает разные множества сетапов для разных сторон.

ЧТО МЕРЯЕТСЯ. Три способа нормировки, каждый на обоих периодах:

    end     нынешний — от конца импульса, перекошен в пользу шортов;
    start   от начала — перекошен в другую сторону, ровно так же;
    mid     от середины — единственный симметричный.

Если разница между ними в пределах шума, вопрос закрыт: перекос есть, но он
ни на что не влияет. Если нет — нормировку надо менять на симметричную, и
все прежние выводы по сторонам придётся перечитать.

ПРИЁМКА здесь особая. Меня интересует не «что лучше», а «отличаются ли»: это
проверка на существование артефакта, а не поиск улучшения. Поэтому смотрю на
разницу средних и на то, накрывает ли её интервал ноль.

Запуск:
    python research/fibo_velocity.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS  # noqa: E402
from fibo_audit import ci, diff_ci, hush, unhush  # noqa: E402

PAIRS_LIMIT = 8
MODES = ('end', 'start', 'mid')


def collect(pair, data, mode):
    """Сетапы боевой стратегии с размером, посчитанным нужной нормировкой."""
    import config
    import strategy
    from smc_engine import Order

    df_1h = data['1h']
    lookback = config.LOOKBACK_CANDLES
    expiry = np.timedelta64(int(config.PENDING_ORDER_MAX_HOURS * 3600), 's')

    out, seen = [], set()
    for i in range(lookback + 10, len(df_1h)):
        window = df_1h.iloc[i - lookback: i + 1]
        signal = strategy.analyze_market(window, None, pair, 10_000)
        if not signal:
            continue
        setup, prm = signal['setup'], signal['params']

        # Пересчёт порога скорости с нужной базой. Боевой поиск уже применил
        # свою, поэтому здесь ДОПОЛНИТЕЛЬНО отсеиваем то, что при другой базе
        # порог бы не прошло. Так сравниваются именно нормировки: множество
        # сетапов у каждой получается своё.
        start_price = float(setup['start_price'])
        end_price = float(setup['end_price'])
        size = float(setup['size'])
        base = {'end': end_price, 'start': start_price,
                'mid': (start_price + end_price) / 2}[mode]
        candles = setup.get('candles') or 1
        if (size / base * 100) / candles < config.MIN_IMPULSE_VELOCITY:
            continue
        if size / base * 100 < config.MIN_IMPULSE_PCT:
            continue

        key = (pair, setup['type'], round(start_price, 8), round(end_price, 8))
        if key in seen:
            continue
        seen.add(key)
        created = pd.Timestamp(window.iloc[-1]['timestamp']) \
            .tz_convert('UTC').tz_localize(None).to_datetime64()
        out.append(Order(
            pair=pair, direction=setup['type'],
            entry=prm['entry'], stop=prm['stop_loss'],
            targets=[prm['take_profit_1']], fractions=[1.0],
            created=created, expires=created + expiry, key=key,
            be_trigger=prm['be_level'] if config.BREAKEVEN_AT_B else None,
            meta={'direction': setup['type']}))
    return out


def run(orders, exec_data):
    import config
    from smc_engine import compute_stats, run_portfolio

    if len(orders) < 5:
        return None
    result = run_portfolio(
        orders, exec_data,
        risk_pct=config.RISK_PER_TRADE,
        max_positions=getattr(config, 'MAX_OPEN_POSITIONS', 5),
        cooldown_hours=getattr(config, 'COOLDOWN_HOURS', 12),
        max_same_direction=getattr(config, 'MAX_SAME_DIRECTION', 0),
        breakeven_after_tp1=False)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 5:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    longs = [t['pnl'] / t['risk'] for t in trades
             if (t.get('meta') or {}).get('direction') == 'LONG']
    shorts = [t['pnl'] / t['risk'] for t in trades
              if (t.get('meta') or {}).get('direction') == 'SHORT']
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'mean': float(r.mean()), 'total': float(r.sum()),
            'dd': stats['max_dd_pct'],
            'longs': np.array(longs), 'shorts': np.array(shorts)}


def main():
    os.environ.setdefault('SMC_CACHE_DIR', BULL_CACHE)
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
        periods[label] = data

    results = {}
    for label, data in periods.items():
        print()
        print('=' * 100)
        print(f'{label}')
        print('=' * 100)
        head = (f'{"нормировка":<14}{"заявок":>8}{"сделок":>8}{"лонгов":>8}'
                f'{"шортов":>8}{"R/сделку":>10}{"R лонг":>9}{"R шорт":>9}'
                f'{"сумма R":>9}{"DD%":>7}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        quiet = hush()
        try:
            for mode in MODES:
                orders = []
                for pair in data:
                    orders += collect(pair, data[pair], mode)
                res = run(orders, {p: data[p]['5m'] for p in data})
                if res is None:
                    print(f'{mode:<14}{"— мало сделок":>16}')
                    continue
                results[label][mode] = res
                tag = mode + (' (сейчас)' if mode == 'end' else '')
                ml = res['longs'].mean() if len(res['longs']) else float('nan')
                ms = res['shorts'].mean() if len(res['shorts']) else float('nan')
                print(f'{tag:<14}{res["orders"]:>8}{res["n"]:>8}'
                      f'{len(res["longs"]):>8}{len(res["shorts"]):>8}'
                      f'{res["mean"]:>10.3f}{ml:>9.3f}{ms:>9.3f}'
                      f'{res["total"]:>9.1f}{res["dd"]:>7.1f}')
        finally:
            unhush(quiet)

    print()
    print('=' * 100)
    print('ОТЛИЧАЮТСЯ ЛИ НОРМИРОВКИ (интервал разницы средних с нынешней)')
    print('=' * 100)
    for mode in MODES:
        if mode == 'end':
            continue
        for label, table in results.items():
            res, ref = table.get(mode), table.get('end')
            if not res or not ref:
                continue
            lo, hi = diff_ci(res['r'], ref['r'])
            gap = res['mean'] - ref['mean']
            verdict = 'РАЗЛИЧИЕ' if (lo > 0 or hi < 0) else 'шум'
            print(f'{mode:<8}{label:<18}{gap:>+8.3f} [{lo:+.3f}; {hi:+.3f}]'
                  f'   сделок {res["n"]} против {ref["n"]}   {verdict}')

    print()
    print('Если везде «шум» — перекос в нормировке существует, но ни на что не')
    print('влияет, и трогать боевой код незачем. Менять работающую формулу ради')
    print('красоты — это риск без выигрыша.')


if __name__ == '__main__':
    main()
