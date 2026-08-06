"""
Ложный пробой уровня: торговля ПРОТИВ выхода за уровень.

ОТКУДА ВЗЯЛАСЬ ГИПОТЕЗА — И ЧЕСТНАЯ ОГОВОРКА К НЕЙ.

Замер scalp_breakout.py закрыл идею пробоя: шесть вариантов, два периода,
двадцать четыре тысячи сетапов — все шесть в глубоком минусе, -0.20…-0.33 R
на сделку. Издержки при этом 0.11-0.21 R, то есть ДО издержек результат уже
отрицательный: дело не в комиссиях, движение само не продолжается.

Отрицательный край в одну сторону — это подсказка про другую сторону, но
НЕ доказательство. Перевернуть сделку не значит перевернуть знак: стоп и
цель меняются местами, а с ними и отношение риска к прибыли. Сделка с RR 2
в обратную сторону даёт RR 0.5, и тот же процент попаданий даст совсем
другой итог. Поэтому обратная сторона меряется отдельно, со своей
геометрией, а не выводится арифметикой из первого замера.

Вторая оговорка, важнее первой. Гипотеза РОДИЛАСЬ ИЗ ЭТИХ ЖЕ ДАННЫХ, а
значит склонна на них же и подтвердиться. Двусторонняя приёмка — два
независимых периода — тут не страховка от подгонки, а минимальное
требование. Если разница между периодами окажется большой даже при обоих
плюсах, доверия варианту нет.

ДВЕ ФОРМУЛИРОВКИ, И ОНИ РАЗНЫЕ ПО СМЫСЛУ.

    инверсия        встаём против пробоя сразу на его закрытии. Никакого
                    подтверждения: ставка ровно на то, что выход за уровень
                    систематически не продолжается.

    возврат внутрь  ждём, пока цена ЗАКРОЕТСЯ обратно за уровнем в течение
                    нескольких баров, и только тогда встаём против. Это
                    классический несостоявшийся пробой: сначала за уровнем
                    собрали стопы, потом цена вернулась — и вернувшихся
                    ловят. Сделок меньше, но каждая с подтверждением.

СТОП И ЦЕЛЬ. Стоп — за экстремум самого выхода за уровень плюс буфер: если
цена его обновила, пробой всё-таки состоялся, и мы неправы. Цель —
противоположная граница прижатия: возврат внутрь диапазона отрабатывает
именно туда, где стоит встречная ликвидность.

РЕЗУЛЬТАТ, 2026-08-06. ОТРИЦАТЕЛЬНЫЙ. Три варианта, два периода, 24 564
сетапа — все в минусе, интервалы ноль не накрывают:

    инверсия · цель граница прижатия   -0.235 [-0.281; -0.188]   -0.213
    инверсия · цель уровень            -0.292 [-0.452; -0.124]   -0.334
    возврат внутрь · цель прижатия     -0.212 [-0.333; -0.090]   -0.145

Оговорка из шапки отработала: минус -0.256 не стал плюсом 0.256, он стал
минусом 0.235. Но переворот всё-таки сдвинул край, и это единственное
содержательное, что здесь удалось измерить. Валовый результат, до издержек:

    пробой         -0.256 + 0.150 = -0.106
    против пробоя  -0.235 + 0.196 = -0.039

Сторона против пробоя действительно ближе к нулю — направление подсказки
было верным. До нуля она не дошла, а издержки в 0.17-0.20 R с каждой сделки
дальше решают всё сами.

Отсюда вывод, который стоило записать давно: на пятиминутках со стопом около
процента издержки — не поправка к результату, а его главное слагаемое. Идея
обязана иметь валовый край СИЛЬНО больше 0.2 R, иначе арифметика съедает её
целиком, как бы красива ни была логика.

СЕМЕЙСТВО ЗАКРЫТО: уровень с прижатием, две формулировки входа, обе стороны,
два независимых периода. Третью формулировку искать не нужно — это была бы
подгонка под одни и те же данные, о чём выше написано заранее.

Запуск:
    python research/scalp_fade.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fibo_audit import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402
from scalp_breakout import PAIRS_LIMIT, collect_setups  # noqa: E402

# ── Параметры этого замера, свои ─────────────────────────────────────────────
# Сколько баров ждём возврата внутрь. Пять баров на 5-минутках — 25 минут:
# пробой, который не отменился за полчаса, скорее настоящий.
FADE_MAX_BARS = 5
# Буфер за экстремум выхода. Меньше, чем у пробойной версии: здесь стоп стоит
# за уже показанным экстремумом, а не за уровнем, и запас нужен только на шум.
FADE_PAD_ATR = 0.15
# Пол по расстоянию до стопа — та же арифметика издержек, что и везде:
# круг стоит 0.21% от объёма, при стопе 0.3% это 0.70 R с каждой сделки.
FADE_MIN_STOP_PCT = 0.5
FADE_MIN_RR = 1.2


def _flip(side):
    return 'SHORT' if side == 'LONG' else 'LONG'


def build_orders(setups, frames, mode, target_mode, min_rr=FADE_MIN_RR):
    """
    Заявки против пробоя.

    mode         'invert'  — вход на закрытии пробойной свечи;
                 'return'  — вход после закрытия обратно за уровень.
    target_mode  'box'     — противоположная граница прижатия;
                 'level'   — сам пробитый уровень (ближе, зато чаще берётся).
    """
    from smc_engine import Order

    orders = []
    for s in setups:
        df = frames[s['pair']]
        high = df['_high']
        low = df['_low']
        close = df['_close']
        stamps = df['_time']
        at = s['at']
        side = _flip(s['direction'])          # против пробоя
        level = s['level']
        pad = FADE_PAD_ATR * s['atr']

        if mode == 'invert':
            i = at
        else:
            # Ищем закрытие ОБРАТНО за уровень. Только закрытие: тень внутрь
            # диапазона — это и есть обычный шум пробойного бара.
            i = None
            for j in range(at + 1, min(at + 1 + FADE_MAX_BARS, len(close))):
                back = (close[j] < level) if s['direction'] == 'LONG' \
                    else (close[j] > level)
                if back:
                    i = j
                    break
            if i is None:
                continue

        # Экстремум всего выхода за уровень — от пробойной свечи до входа.
        span_hi = float(np.max(high[at:i + 1]))
        span_lo = float(np.min(low[at:i + 1]))
        entry = float(close[i])

        if side == 'SHORT':
            stop = max(span_hi + pad, entry * (1 + FADE_MIN_STOP_PCT / 100))
            target = s['box_low'] if target_mode == 'box' else level
        else:
            stop = min(span_lo - pad, entry * (1 - FADE_MIN_STOP_PCT / 100))
            target = s['box_high'] if target_mode == 'box' else level

        distance = abs(entry - stop)
        if distance <= 0:
            continue
        reward = abs(target - entry)
        rr = reward / distance
        if rr < min_rr:
            continue
        # Цель обязана лежать по ходу сделки: при глубоком заходе за уровень
        # противоположная граница может оказаться уже пройденной.
        if side == 'SHORT' and target >= entry:
            continue
        if side == 'LONG' and target <= entry:
            continue

        created = stamps[i]
        orders.append(Order(
            pair=s['pair'], direction=side,
            entry=entry, stop=stop, targets=[target], fractions=[1.0],
            created=created, expires=created + np.timedelta64(2 * 5 * 60, 's'),
            key=(s['pair'], side, round(level, 8), int(at)),
            entry_type='stop',
            meta={'rr': rr, 'stop_pct': distance / entry * 100,
                  'direction': side, 'waited': i - at},
        ))
    return orders


def run_variant(setups, frames, data, mode, target_mode):
    from scalp import params
    from smc_engine import compute_stats, run_portfolio

    orders = build_orders(setups, frames, mode, target_mode)
    if not orders:
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
    longs = sum(1 for t in trades if (t.get('meta') or {}).get('direction') == 'LONG')
    return {'r': r, 'n': len(trades), 'mean': float(r.mean()),
            'total': float(r.sum()), 'wr': float((r > 0).mean() * 100),
            'costs': float(costs.mean()), 'orders': len(orders), 'longs': longs,
            'dd': stats['max_dd_pct'], 'ret': stats['return_pct']}


def load(cache_dir, pairs, label):
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


VARIANTS = [
    ('инверсия · цель граница прижатия',       'invert', 'box'),
    ('инверсия · цель уровень',                'invert', 'level'),
    ('возврат внутрь · цель граница прижатия', 'return', 'box'),
    ('возврат внутрь · цель уровень',          'return', 'level'),
]


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        periods[label] = load(cache, pairs, label)

    results = {}
    for label, (data, frames, setups) in periods.items():
        print()
        print('=' * 108)
        print(f'{label}   сетапов: {len(setups)}   пар: {len(data)}')
        print('=' * 108)
        head = (f'{"вариант":<42}{"заявок":>8}{"сделок":>8}{"лонгов":>8}'
                f'{"винрейт":>9}{"R/сделку":>10}{"издержки":>10}{"сумма R":>9}'
                f'{"DD%":>7}{"интервал":>24}')
        print(head)
        print('-' * len(head))
        results[label] = {}
        for name, mode, target_mode in VARIANTS:
            res = run_variant(setups, frames, data, mode, target_mode)
            if res is None:
                print(f'{name:<42}{"— сделок нет":>16}')
                continue
            results[label][name] = res
            lo, hi = ci(res['r'])
            print(f'{name:<42}{res["orders"]:>8}{res["n"]:>8}{res["longs"]:>8}'
                  f'{res["wr"]:>8.1f}%{res["mean"]:>10.3f}{res["costs"]:>10.3f}'
                  f'{res["total"]:>9.1f}{res["dd"]:>7.1f}'
                  f'{f"[{lo:+.3f}; {hi:+.3f}]":>24}')

    print()
    print('=' * 108)
    print('ПРИЁМКА: в плюсе на ОБОИХ периодах и интервал не накрывает ноль')
    print('=' * 108)
    for name, _m, _t in VARIANTS:
        cells, verdicts = '', []
        for label, table in results.items():
            res = table.get(name)
            if not res:
                cells += f'{"—":>30}'
                verdicts.append(False)
                continue
            lo, hi = ci(res['r'])
            verdicts.append(res['mean'] > 0 and lo > 0)
            cell = f'{res["mean"]:+.3f} [{lo:+.3f}; {hi:+.3f}] n={res["n"]}'
            cells += f'{cell:>30}'
        mark = '  ПРИНЯТ' if all(verdicts) and verdicts else ''
        print(f'{name:<42}{cells}{mark}')

    print()
    print('Гипотеза выросла из этих же данных, поэтому мало «в плюсе на обоих».')
    print('Смотреть надо и на РАЗБРОС между периодами: если средние отличаются')
    print('вдвое, вариант держится на особенностях периода, а не на крае.')


if __name__ == '__main__':
    main()
