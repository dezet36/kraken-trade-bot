"""
Волновая разметка: распределение до назначения порогов.

Отвечает на четыре вопроса, без которых любой порог был бы назначен на глаз:

    1. сколько пивотов даёт зигзаг при разных порогах разворота;
    2. какое ЗАПАЗДЫВАНИЕ у разметки — сколько баров проходит между самим
       экстремумом и моментом, когда он подтверждён (источники утверждают, что
       именно здесь умирает вся идея: при крупном пороге счёт становится
       известен, когда движение уже закончилось);
    3. как распределена глубина волны 2 — совпадает ли она с каноном 0.5-0.618;
    4. как часто откат нарушает правило 1, то есть как часто разметка сама себя
       отменяет.

Ничего не торгуется. Запуск:
    python research/wave_probe.py
"""

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import BULL_CACHE, BULL_PAIRS  # noqa: E402

PAIRS_LIMIT = 6
THRESHOLDS = (1.0, 1.5, 2.0, 2.5, 3.5, 5.0)


def q(values, *points):
    if len(values) == 0:
        return [float('nan')] * len(points)
    return [float(np.percentile(values, p)) for p in points]


def main():
    os.environ['SMC_CACHE_DIR'] = BULL_CACHE
    import backtest_smc as bt
    from wave import core

    frames = []
    for pair in BULL_PAIRS[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded and '1h' in loaded:
            frames.append((pair, loaded['1h']))
    bars = sum(len(df) for _, df in frames)
    print(f'пар: {len(frames)}   часовых баров: {bars}')
    print()

    head = (f'{"порог":>7}{"пивотов":>10}{"на 1000 баров":>15}{"колено ATR":>12}'
            f'{"запаздывание баров":>21}{"доля колена":>13}')
    print(head)
    print('-' * len(head))

    per_threshold = {}
    for thr in THRESHOLDS:
        pivots_total, legs, lags, lag_frac = 0, [], [], []
        waves = []
        for pair, df in frames:
            high = df['high'].to_numpy(float)
            low = df['low'].to_numpy(float)
            close = df['close'].to_numpy(float)
            atr = core.atr_series(high, low, close)
            pv = core.zigzag(high, low, close, reversal_atr=thr, atr=atr)
            pivots_total += len(pv)
            for j in range(1, len(pv)):
                a, b = pv[j - 1], pv[j]
                size = abs(b['price'] - a['price'])
                atr_at = atr[b['confirmed_at']]
                if np.isfinite(atr_at) and atr_at > 0:
                    legs.append(size / atr_at)
                lag = b['confirmed_at'] - b['index']
                span = max(b['index'] - a['index'], 1)
                lags.append(lag)
                lag_frac.append(lag / span)
            waves.append((pv, atr))
        per_threshold[thr] = waves

        lo_leg, mid_leg, hi_leg = q(legs, 25, 50, 75)
        lag_med, lag_hi = q(lags, 50, 90)
        frac_med = q(lag_frac, 50)[0]
        print(f'{thr:>7.1f}{pivots_total:>10}'
              f'{pivots_total / bars * 1000:>15.1f}'
              f'{f"{lo_leg:.1f}/{mid_leg:.1f}/{hi_leg:.1f}":>12}'
              f'{f"{lag_med:.0f} / 90%: {lag_hi:.0f}":>21}'
              f'{frac_med:>12.0%}')

    print()
    print('колено ATR и запаздывание — 25/50/75 процентили и медиана.')
    print('«доля колена» — какую часть длины волны 1 занимает ожидание её')
    print('подтверждения. Если это половина, торговать по разметке поздно.')

    print()
    print('=' * 78)
    print('ВОЛНА 2: глубина отката и как часто нарушается правило 1')
    print('=' * 78)
    head = (f'{"порог":>7}{"троек":>9}{"правило 1 нарушено":>21}'
            f'{"глубина 25/50/75":>22}{"в каноне 0.5-0.618":>21}')
    print(head)
    print('-' * len(head))
    for thr in THRESHOLDS:
        depths, broken, total = [], 0, 0
        for pv, atr in per_threshold[thr]:
            for k in range(2, len(pv)):
                a, b, c = pv[k - 2], pv[k - 1], pv[k]
                if a['kind'] != c['kind'] or a['kind'] == b['kind']:
                    continue
                w1 = abs(b['price'] - a['price'])
                if w1 <= 0:
                    continue
                total += 1
                depth = ((b['price'] - c['price']) if a['kind'] == 'L'
                         else (c['price'] - b['price']))
                r = depth / w1
                if r >= 1.0:
                    broken += 1
                else:
                    depths.append(r)
        if total == 0:
            continue
        d25, d50, d75 = q(depths, 25, 50, 75)
        canon = float(np.mean([(0.5 <= d <= 0.618) for d in depths])) if depths else 0
        print(f'{thr:>7.1f}{total:>9}{broken / total:>20.0%}'
              f'{f"{d25:.2f} / {d50:.2f} / {d75:.2f}":>22}{canon:>20.0%}')

    print()
    print('Если «правило 1 нарушено» велико, разметка отменяется чаще, чем')
    print('отрабатывает, и вход лимитом будет систематически ловить те откаты,')
    print('которые уже не являются волной 2.')


if __name__ == '__main__':
    main()
