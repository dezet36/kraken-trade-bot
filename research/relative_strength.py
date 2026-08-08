"""
Относительная сила между парами: есть ли что ранжировать и покрывает ли это круг.

ЧЕМ ЭТОТ ВОПРОС ОТЛИЧАЕТСЯ ОТ ВСЕГО, ЧТО МЫ МЕРИЛИ. Семь закрытых за день
семейств спрашивали одно: куда пойдёт инструмент, судя по форме его прошлой
цены. Здесь вопрос другой — какая из пар обгонит остальные. Абсолютное
направление не нужно вовсе: покупается верх списка, продаётся низ, и общее
движение рынка гасится само.

У этой идеи, в отличие от волн Эллиотта, есть настоящая доказательная база:
кросс-секционный моментум воспроизведён на акциях, товарах и валютах в
рецензируемых работах за десятилетия. Это не гарантия, что он работает на
крипте, но это принципиально другая исходная позиция.

СНАЧАЛА АРИФМЕТИКА, ПОТОМ СТРАТЕГИЯ. Тот же порядок, что решил судьбу торговли
по стакану (спред 0.0002% против комиссии круга 0.04% — идея умерла за час) и
задал масштаб Боллинджеру (издержки равны кругу, делённому на стоп). Здесь
сделка ДВОЙНАЯ — лонг и шорт одновременно, — значит и кругов два:

    тейкер с обеих сторон   2 × 0.210% = 0.420%
    мейкер с обеих сторон   2 × 0.040% = 0.080%

Если разброс доходностей между парами меньше этого, стратегии нет, и никакая
геометрия входа её не создаст.

ЧТО МЕРЯЕТСЯ, БЕЗ СТОПОВ И БЕЗ ПОРТФЕЛЯ
    1. РАЗБРОС: насколько вообще расходятся пары за окно. Если крипта ходит
       одним куском, ранжировать нечего.
    2. ПРОГНОЗ: даёт ли ранг по прошлой доходности разницу в БУДУЩЕЙ. Берём
       верхние k и нижние k, считаем разность их доходности вперёд.
    3. КОНТРОЛЬ: то же самое при СЛУЧАЙНОМ ранге. Тот же приём, что и с
       плацебо у волн: разница, неотличимая от случайной, разницей не является.

Стопы, портфель и издержки в модели появятся только если это пройдёт. Иначе
получится то, о чём предупреждал разбор волн: красивая геометрия поверх
отсутствующего края.

Запуск:
    python research/relative_strength.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BEAR_CACHE, BEAR_PAIRS, BULL_CACHE, BULL_PAIRS, ci  # noqa: E402

PAIRS_LIMIT = 20
LOOKBACKS = (4, 12, 24, 72, 168)      # часов, по которым ранжируем
HORIZONS = (4, 12, 24, 72)            # часов, на которые смотрим вперёд
SIDE = 3                              # сколько пар берём сверху и снизу
RNG = np.random.default_rng(20260808)

COST_TAKER = 0.420                    # два круга тейкером, %
COST_MAKER = 0.080                    # два круга мейкером, %


def load(cache_dir, pairs, label):
    """Матрица закрытий: строки — время, столбцы — пары."""
    os.environ['SMC_CACHE_DIR'] = cache_dir
    sys.modules.pop('backtest_smc', None)
    import backtest_smc as bt

    print(f'[{label}] загрузка...', flush=True)
    frames = {}
    for pair in pairs[:PAIRS_LIMIT]:
        loaded = bt.load_pair(pair)
        if loaded is None or '1h' not in loaded:
            continue
        df = loaded['1h']
        stamps = pd.to_datetime(df['timestamp'])
        if getattr(stamps.dt, 'tz', None) is not None:
            stamps = stamps.dt.tz_convert('UTC').dt.tz_localize(None)
        frames[pair] = pd.Series(df['close'].to_numpy(float),
                                 index=stamps.to_numpy())
    if not frames:
        return None
    # Внутреннее соединение по времени: сравнивать пары можно только на общих
    # барах. Пара, появившаяся позже остальных, иначе тянула бы за собой
    # пустоты и портила ранг.
    matrix = pd.DataFrame(frames).dropna()
    print(f'      пар {matrix.shape[1]}, общих баров {matrix.shape[0]}',
          flush=True)
    return matrix


def dispersion(matrix, window):
    """
    Разброс доходностей между парами, в процентах.

    Это верхняя граница всего, на что можно рассчитывать: если пары за окно
    расходятся на десятые доли процента, разность верха и низа списка не
    покроет и одного круга комиссий.
    """
    ret = matrix.pct_change(window).dropna() * 100
    spread = ret.max(axis=1) - ret.min(axis=1)
    top_bottom = []
    values = ret.to_numpy()
    for row in values:
        order = np.argsort(row)
        top_bottom.append(row[order[-SIDE:]].mean() - row[order[:SIDE]].mean())
    return {
        'std': float(ret.std(axis=1).median()),
        'full': float(np.median(spread)),
        'k': float(np.median(top_bottom)),
        'n': len(ret),
    }


def forward_spread(matrix, lookback, horizon, shuffle=False):
    """
    Разность будущей доходности верха и низа рейтинга, в процентах на сделку.

    Ранжируем по доходности за `lookback`, смотрим на `horizon` вперёд. При
    shuffle ранг случайный — это контроль: он отвечает, ранжирование ли даёт
    разницу или её дала бы любая произвольная тройка пар.
    """
    past = matrix.pct_change(lookback)
    future = matrix.shift(-horizon) / matrix - 1
    both = past.notna() & future.notna()
    rows = []
    past_v, future_v, mask = past.to_numpy(), future.to_numpy(), both.to_numpy()
    # Шаг равен горизонту: соседние наблюдения иначе перекрываются, и
    # доверительный интервал получился бы уже правды в разы.
    for i in range(lookback, len(matrix) - horizon, horizon):
        keep = mask[i]
        if keep.sum() < SIDE * 2 + 1:
            continue
        p, f = past_v[i][keep], future_v[i][keep]
        order = RNG.permutation(len(p)) if shuffle else np.argsort(p)
        top = f[order[-SIDE:]].mean()
        bottom = f[order[:SIDE]].mean()
        rows.append((top - bottom) * 100)
    return np.array(rows, dtype=float)


def main():
    periods = {}
    for label, cache, pairs in (('бык 2025-26', BULL_CACHE, BULL_PAIRS),
                                ('медведь 2022-23', BEAR_CACHE, BEAR_PAIRS)):
        matrix = load(cache, pairs, label)
        if matrix is not None:
            periods[label] = matrix

    print()
    print('=' * 96)
    print('1. РАЗБРОС МЕЖДУ ПАРАМИ — есть ли что ранжировать')
    print('=' * 96)
    print(f'{"период":<18}{"окно":>6}{"откл.":>9}{"размах":>9}'
          f'{"верх−низ":>11}{"наблюдений":>12}   круг мейкером 0.080%')
    print('-' * 96)
    for label, matrix in periods.items():
        for window in LOOKBACKS:
            d = dispersion(matrix, window)
            verdict = 'покрывает' if d['k'] > COST_MAKER * 2 else 'ТОНКО'
            print(f'{label:<18}{window:>5}ч{d["std"]:>9.2f}{d["full"]:>9.2f}'
                  f'{d["k"]:>11.2f}{d["n"]:>12}   {verdict}')
    print()
    print('«верх−низ» — медианная разность средней доходности трёх лучших и')
    print('трёх худших пар за окно. Это и есть материал, из которого стратегия')
    print('могла бы взять свою прибыль. Круг мейкером стоит 0.080% (две ноги).')

    print()
    print('=' * 96)
    print('2. ПРОГНОЗ: даёт ли прошлый ранг разницу в БУДУЩЕМ')
    print('=' * 96)
    for label, matrix in periods.items():
        print()
        print(f'--- {label} ---')
        head = (f'{"окно":>6}{"вперёд":>8}{"сделок":>8}{"разность %":>12}'
                f'{"интервал":>24}{"случайно %":>12}{"чистыми %":>11}')
        print(head)
        print('-' * len(head))
        for lookback in LOOKBACKS:
            for horizon in HORIZONS:
                real = forward_spread(matrix, lookback, horizon)
                fake = forward_spread(matrix, lookback, horizon, shuffle=True)
                if len(real) < 30:
                    continue
                lo, hi = ci(real)
                net = real.mean() - COST_MAKER
                print(f'{lookback:>5}ч{horizon:>7}ч{len(real):>8}'
                      f'{real.mean():>12.3f}{f"[{lo:+.3f}; {hi:+.3f}]":>24}'
                      f'{fake.mean():>12.3f}{net:>11.3f}')

    print()
    print('=' * 96)
    print('КАК ЧИТАТЬ. «Разность» — сколько процентов даёт связка «лонг верх,')
    print('шорт низ» за горизонт, ДО издержек и без стопов. «Случайно» — то же')
    print('при перемешанном ранге; если числа рядом, ранжирование не работает.')
    print('«Чистыми» — за вычетом круга мейкером 0.080% на обе ноги.')
    print()
    print('Тема живёт дальше только если чистая разность положительна на ОБОИХ')
    print('периодах, интервал отделён от нуля и результат заметно выше')
    print('случайного. Иначе строить стратегию не из чего.')


if __name__ == '__main__':
    main()
