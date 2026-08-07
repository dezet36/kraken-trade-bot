"""
Скальпинг по RSI и полосам Боллинджера: канон против обратного прочтения.

ЧТО ГОВОРИТ ПРОБНИК (research/rsibb_probe.py, 691 212 пятиминутных баров,
6 пар, ТОЛЬКО бычий период) — и это переворачивает задачу.

    доля случаев, когда цена вернулась к средней линии
    без фильтров                         26%
    RSI 30/70 (канон)                    16%     −10.6 п.п.
    RSI 25/75 (канон, строже)            17%      −9.6 п.п.
    ADX < 25                             26%      −0.2 п.п.
    ADX < 20                             26%      +0.1 п.п.
    RSI 30/70 и ADX < 25                  6%     −20.2 п.п.
    RSI нейтральный 40-60                39%     +12.5 п.п.
    расхождение (лонг при RSI > 45)      55%     +28.9 п.п.
    расхождение (лонг при RSI > 50)      76%     +50.0 п.п.

Три вывода, каждый против учебника.

1. ADX НЕ РАБОТАЕТ ВООБЩЕ. Источники называют его тем, что делает стратегию
   торгуемой («с фильтрами 58-65% попаданий, без них 45%»). На наших данных он
   не двигает исход ни на процент, при любом пороге. Это уже второй случай,
   когда сильное внешнее утверждение с числами не воспроизводится: в замере
   пробоя пересыхание объёма обещало 65% против 48% и дало +0.005 R.

2. КАНОНИЧЕСКИЙ RSI ДЕЛАЕТ ХУЖЕ, А НЕ ЛУЧШЕ. Объяснение простое: RSI на полосе
   помечает не истощение продавца, а действующий импульс — то есть ровно ту
   ходьбу по полосе, от которой фильтр должен был защищать. Каноническая связка
   RSI+ADX даёт 6% возвратов против 26% без всякого отбора: это худшее, что
   можно построить из этих двух индикаторов.

3. ОБРАТНОЕ ПРОЧТЕНИЕ ДАЁТ +50 п.п., И ИМЕННО ПОЭТОМУ ЕМУ НЕЛЬЗЯ ВЕРИТЬ.

ПОДОЗРЕНИЕ ЗАПИСАНО ДО ПРОГОНА, ЧТОБЫ НЕЛЬЗЯ БЫЛО ОБЪЯСНИТЬ ЗАДНИМ ЧИСЛОМ.
Касание нижней полосы — это обычно ТЕНЬ. Если RSI при этом выше 50, значит
закрытия росли, то есть рынок в восходящем тренде, а вниз ушла одна тень.
«Возврат к средней линии» в восходящем тренде — просто продолжение роста.
Тогда «расхождение» отбирает не истощение продавца, а прокол вниз внутри
тренда, и это уже не возврат к среднему, а следование за трендом в другой
одежде. Пробник считался ТОЛЬКО на бычьем периоде, где такое вознаграждается
по построению.

    Если подозрение верно, расхождение развалится на медвежьем периоде
    ЛИБО сохранится, но перекосится в шорты. И то и другое видно в таблице.

АРИФМЕТИКА ИЗДЕРЖЕК ПРОВЕРЕНА И ОНА ПРОХОДИТ. Медианная полуширина канала
0.461% цены, то есть цель на средней линии стоит 0.46%. Круг мейкер-мейкер
0.040% съедает 9% цели, тейкер-тейкер 0.210% — 46%. Вход лимитом на полосе и
цель лимитом на средней — обе заявки мейкерские по построению, поэтому
стратегия арифметически жизнеспособна. Это отличает её от пробоя, где издержки
съедали весь край, но НЕ отличает от сетки в коридоре, где та же арифметика
сходилась, а валовый край всё равно был отрицательным.

ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА И НЕ ПОДЛЕЖАЩАЯ СМЯГЧЕНИЮ:

    в плюсе на ОБОИХ периодах, доверительный интервал не накрывает ноль
    И просадка не больше 25% на обоих.

ИМЯ ФАЙЛА НЕ rsibb.py СОЗНАТЕЛЬНО. Каталог замеров стоит в пути раньше
Live_Bot, и файл, названный как пакет стратегии, заслонил бы его собой:
`from rsibb import core` нашёл бы этот самый скрипт.

Запуск:
    python research/bollinger_rsi.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

PAIRS_LIMIT = 8
TIMEFRAME = '5m'
BAR_MIN = 5

BASE = {
    'rsi_mode': 'extreme', 'rsi_low': 30.0, 'rsi_high': 70.0,
    'adx_max': 0.0, 'max_width_ratio': 0.0,
    'entry_mode': 'touch', 'target_frac': 1.0, 'stop_frac': 0.5,
}

VARIANTS = [
    # ── Канон, как в учебнике ──────────────────────────────────────────────
    ('канон: RSI 30/70, цель на средней',      {}),
    ('канон строже: RSI 25/75',                {'rsi_low': 25, 'rsi_high': 75}),
    ('канон + ADX < 25',                       {'adx_max': 25}),
    ('канон + ADX < 20',                       {'adx_max': 20}),

    # ── Контроль: а нужен ли RSI вообще ────────────────────────────────────
    ('без RSI — только полоса',                {'rsi_mode': 'off'}),
    ('без RSI + ADX < 25',                     {'rsi_mode': 'off',
                                                'adx_max': 25}),

    # ── Обратное прочтение, к которому есть записанное подозрение ──────────
    ('расхождение: лонг при RSI > 45',         {'rsi_mode': 'divergence',
                                                'rsi_low': 45, 'rsi_high': 55}),
    ('расхождение сильнее: RSI > 50',          {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50}),
    ('нейтральный RSI 40-60',                  {'rsi_mode': 'neutral',
                                                'rsi_low': 40, 'rsi_high': 60}),

    # ── Геометрия выхода ───────────────────────────────────────────────────
    ('расхождение · цель на дальней полосе',   {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'target_frac': 2.0}),
    ('расхождение · стоп 1.0 полуширины',      {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'stop_frac': 1.0}),
    ('расхождение · вход после возврата',      {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'entry_mode': 'reclaim'}),
    ('канон · вход после возврата',            {'entry_mode': 'reclaim'}),

    # ── Фильтр расширения полос ────────────────────────────────────────────
    ('расхождение · полосы не расширяются',    {'rsi_mode': 'divergence',
                                                'rsi_low': 50, 'rsi_high': 50,
                                                'max_width_ratio': 1.1}),
]


def scan(pairs, cache_dir, label):
    """Индикаторы всех пар. Считаются один раз на период."""
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt
    from rsibb import core

    print(f'[{label}] загрузка и индикаторы...', flush=True)
    data, marks = {}, []
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or TIMEFRAME not in loaded:
            continue
        df = loaded[TIMEFRAME]
        data[pair] = df
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        marks.append({
            'pair': pair,
            'ind': core.indicators(df['open'].to_numpy(float),
                                   df['high'].to_numpy(float),
                                   df['low'].to_numpy(float),
                                   df['close'].to_numpy(float)),
            'stamps': stamps.to_numpy(),
        })
        print(f'      {pair}: баров {len(df)}', flush=True)
    return data, marks


def build_orders(marks, cfg):
    from rsibb import core, params
    from smc_engine import Order

    life = np.timedelta64(params.EXPIRY_BARS * BAR_MIN * 60, 's')
    orders, reasons = [], {}
    for mark in marks:
        ind, stamps = mark['ind'], mark['stamps']
        n = len(ind['close'])
        # Пауза между сигналами одной пары: в боковике условие держится
        # десятками баров подряд, и без неё замер посчитал бы один и тот же
        # заход сто раз, раздув и число сделок, и уверенность в результате.
        last = -10 ** 9
        for i in range(60, n - 2):
            if i - last < params.MAX_HOLD_BARS // 4:
                continue
            setup, why = core.evaluate(
                ind, i, rsi_low=cfg['rsi_low'], rsi_high=cfg['rsi_high'],
                adx_max=cfg['adx_max'], max_width_ratio=cfg['max_width_ratio'],
                entry_mode=cfg['entry_mode'], rsi_mode=cfg['rsi_mode'])
            if setup is None:
                key = why.split('(')[0].split(' — ')[0][:34]
                reasons[key] = reasons.get(key, 0) + 1
                continue
            trade = core.build_trade(setup, target_frac=cfg['target_frac'],
                                     stop_frac=cfg['stop_frac'])
            if trade is None:
                reasons['геометрия не годится'] = reasons.get(
                    'геометрия не годится', 0) + 1
                continue
            last = i
            created = stamps[i]
            orders.append(Order(
                pair=mark['pair'], direction=setup['direction'],
                entry=trade['entry'], stop=trade['stop'],
                targets=[trade['target']], fractions=[1.0],
                created=created, expires=created + life,
                key=(mark['pair'], i),
                entry_type='limit' if cfg['entry_mode'] == 'touch' else 'stop',
                meta={'rr': trade['rr'], 'stop_pct': trade['stop_pct'],
                      'rsi': setup['rsi'], 'adx': setup['adx'],
                      'direction': setup['direction']},
            ))
    return orders, reasons


def run(marks, data, cfg):
    from rsibb import params
    from smc_engine import compute_stats, run_portfolio

    orders, _reasons = build_orders(marks, cfg)
    if len(orders) < 20:
        return None
    result = run_portfolio(
        orders, data, risk_pct=params.RISK_PCT,
        max_positions=params.MAX_POSITIONS,
        cooldown_hours=params.COOLDOWN_HOURS,
        max_same_direction=params.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False,
        max_hold_hours=params.MAX_HOLD_BARS * BAR_MIN / 60)
    trades = [t for t in result['trades'] if t.get('risk')]
    if len(trades) < 20:
        return None
    stats = compute_stats(result)
    r = np.array([t['pnl'] / t['risk'] for t in trades], dtype=float)
    costs = np.array([(t.get('fees', 0) + t.get('funding', 0)) / t['risk']
                      for t in trades], dtype=float)
    longs = sum(1 for o in orders if o.direction == 'LONG')
    stop_pct = np.array([(o.meta or {}).get('stop_pct', 0) for o in orders], float)
    return {'r': r, 'n': len(trades), 'orders': len(orders),
            'fill': len(trades) / len(orders) * 100,
            'mean': float(r.mean()), 'gross': float((r + costs).mean()),
            'costs': float(costs.mean()), 'wr': float((r > 0).mean() * 100),
            'total': float(r.sum()), 'dd': stats['max_dd_pct'],
            'longs': longs / len(orders) * 100,
            'stop_pct': float(np.median(stop_pct))}


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = scan(pairs, cache, label)

    results = {}
    for label, (data, marks) in periods.items():
        print()
        print('=' * 124)
        print(f'{label}   пар: {len(data)}')
        print('=' * 124)
        head = (f'{"вариант":<40}{"заявок":>8}{"сделок":>8}{"налив":>7}'
                f'{"стоп %":>8}{"лонг":>6}{"винрейт":>9}{"R вал.":>9}'
                f'{"издер.":>8}{"R/сделку":>10}{"сумма":>8}{"DD%":>7}'
                f'{"интервал":>22}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, override in VARIANTS:
            cfg = dict(BASE, **override)
            res = run(marks, data, cfg)
            if res is None:
                print(f'{name:<40}{"— мало сделок":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<40}{res["orders"]:>8}{res["n"]:>8}{res["fill"]:>6.0f}%'
                  f'{res["stop_pct"]:>8.2f}{res["longs"]:>5.0f}%'
                  f'{res["wr"]:>8.1f}%{res["gross"]:>9.3f}{res["costs"]:>8.3f}'
                  f'{res["mean"]:>10.3f}{res["total"]:>8.1f}{res["dd"]:>7.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>22}')

    print()
    print('=' * 124)
    print('ПРИЁМКА, ЗАПИСАННАЯ ДО ПРОГОНА: в плюсе на ОБОИХ периодах,')
    print('интервал не накрывает ноль И просадка не больше 25% на обоих.')
    print('=' * 124)
    for name, _ in VARIANTS:
        cells, ok = '', []
        for label, table in results.items():
            res = table.get(name)
            if not res:
                cells += f'{"—":>36}'
                ok.append(False)
                continue
            lo, hi = ci(res['r'])
            ok.append(res['mean'] > 0 and lo > 0 and res['dd'] <= 25)
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}] DD {res["dd"]:.0f}%'
            cells += f'{cell:>36}'
        print(f'{name:<40}{cells}{"  ПРИНЯТ" if ok and all(ok) else ""}')

    print()
    print('ПРОВЕРКА ЗАПИСАННОГО ПОДОЗРЕНИЯ. Если «расхождение» держится только')
    print('на быке или резко перекошено по стороне (колонка «лонг»), значит')
    print('оно отбирает прокол внутри тренда, а не истощение движения, и')
    print('называть это возвратом к среднему нельзя. Смотреть надо на две')
    print('колонки сразу: результат по периодам и долю лонгов.')


if __name__ == '__main__':
    main()
