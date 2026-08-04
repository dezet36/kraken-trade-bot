"""
Свинги — фрактальные экстремумы (§2.1 методички).

Swing High — свеча, максимум которой выше максимумов N свечей слева и справа.
Swing Low  — зеркально по минимумам. Цвет свечей значения не имеет, тени
учитываются всегда (прямая цитата методички).

N=1 даёт трёхсвечной свинг (минорный), N=2 — пятисвечной (структурный).
Методичка: «пятисвечные экстремумы всегда выступают структурными элементами
рассматриваемого таймфрейма».

ВАЖНО про lookahead: свинг на индексе i становится ИЗВЕСТЕН только на свече
i+N — до этого правые N свечей ещё не сформированы. Поле `confirmed_at`
хранит этот индекс, и весь код выше по стеку обязан фильтровать свинги по
`confirmed_at <= текущий_индекс`. Без этого бэктест будет подглядывать в
будущее и покажет прибыль, которой в реальности нет.
"""

import numpy as np

from . import params


def find_swings(df, n=None, soft_right=None):
    """
    Находит фрактальные свинги на DataFrame свечей.

    df          — DataFrame с колонками high/low (+ timestamp, если есть)
    n           — сколько свечей с каждой стороны (по умолчанию SWING_N_STRUCT)
    soft_right  — нестрогое сравнение справа: делает видимыми равные вершины.
                  Нужно для поиска EQH/EQL, вредно для чистой структуры.

    Возвращает (highs, lows) — два списка словарей, отсортированных по индексу:
        {'index', 'price', 'time', 'kind', 'confirmed_at'}
    """
    n = params.SWING_N_STRUCT if n is None else n
    soft_right = params.SWING_SOFT_RIGHT if soft_right is None else soft_right

    size = len(df)
    if size < 2 * n + 1:
        return [], []

    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    times = df['timestamp'].to_numpy() if 'timestamp' in df.columns else np.arange(size)

    # Центральная область, где свинг вообще может существовать
    core = slice(n, size - n)
    is_high = np.ones(size - 2 * n, dtype=bool)
    is_low = np.ones(size - 2 * n, dtype=bool)

    centre_h = high[core]
    centre_l = low[core]

    for j in range(1, n + 1):
        # Длина каждого среза = size - 2n, как и у центральной области:
        # j <= n, поэтому границы всегда внутри массива.
        left_h = high[n - j: size - n - j]
        right_h = high[n + j: size - n + j]
        left_l = low[n - j: size - n - j]
        right_l = low[n + j: size - n + j]

        # Слева строго: экстремум должен превосходить всё, что было до него.
        is_high &= centre_h > left_h
        is_low &= centre_l < left_l

        # Справа — строго или нестрого, в зависимости от режима.
        if soft_right:
            is_high &= centre_h >= right_h
            is_low &= centre_l <= right_l
        else:
            is_high &= centre_h > right_h
            is_low &= centre_l < right_l

    highs = [
        {
            'index': int(i + n),
            'price': float(high[i + n]),
            'time': times[i + n],
            'kind': 'high',
            'confirmed_at': int(i + n + n),
        }
        for i in np.flatnonzero(is_high)
    ]
    lows = [
        {
            'index': int(i + n),
            'price': float(low[i + n]),
            'time': times[i + n],
            'kind': 'low',
            'confirmed_at': int(i + n + n),
        }
        for i in np.flatnonzero(is_low)
    ]
    return highs, lows


def merge_swings(highs, lows):
    """
    Сливает максимумы и минимумы в одну хронологическую последовательность.

    НИЧЕГО НЕ ВЫБРАСЫВАЕТ — и это принципиально.

    Раньше здесь схлопывались подряд идущие однотипные свинги: из двух swing
    high без swing low между ними оставался более высокий. Выглядело разумно
    («это один структурный элемент, а не два»), но решение принималось по
    БУДУЩИМ данным: чтобы понять, что второй свинг выше, надо дожить до
    второго свинга. На момент, когда существовал только первый, он был
    настоящим уровнем, и живой бот работал бы именно с ним.

    Замер на 900 свечах: удалялось 64 свинга из 257, на 22% проверенных свечей
    живой бот видел уровни, которых не было в бэктесте, и ТРИ события слома
    структуры бэктест терял целиком. Order-блоки привязаны к сломам, так что
    каждое потерянное событие — это зона и сделка, которых бэктест не видел,
    а бой увидит.

    Схлопывание не нужно и по существу: `build_structure` публикует свинги по
    мере подтверждения и сам заменяет опорный уровень на более свежий. Это и
    есть причинно-корректное «схлопывание» — по мере поступления данных, а не
    задним числом.
    """
    return sorted([*highs, *lows], key=lambda s: (s['index'], s['kind']))


def visible_swings(swings, at_index):
    """
    Фильтр против подглядывания в будущее: оставляет только свинги, которые
    к свече `at_index` уже подтверждены правыми N свечами.
    """
    return [s for s in swings if s['confirmed_at'] <= at_index]


def last_swing(swings, kind, at_index=None):
    """Последний свинг заданного типа ('high'/'low'), видимый на at_index."""
    pool = swings if at_index is None else visible_swings(swings, at_index)
    for swing in reversed(pool):
        if swing['kind'] == kind:
            return swing
    return None
