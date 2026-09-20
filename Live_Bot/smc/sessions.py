"""
Торговые сессии и открытия старших таймфреймов (§11 методички).

Killzone — высоковероятный временной период для поиска сетапа. §11.2:
«все сделки рекомендуется открывать во время killzone. Вне сессий, на
выходных и в праздники волатильность ниже и предсказуемость хуже из-за
отсутствия на рынке институциональных игроков».

Методичка задаёт время в UTC+2, здесь всё пересчитано в UTC (см. params).

    Asia Open    02:00-07:00 UTC+2 = 00:00-05:00 UTC — формирование рenджа
    London Open  09:00-12:00 UTC+2 = 07:00-10:00 UTC — часто хай/лоу дня
    New York     14:00-17:00 UTC+2 = 12:00-15:00 UTC — основное движение
    London Close 17:00-19:00 UTC+2 = 15:00-17:00 UTC — коррекция 20-30%

Также здесь азиатский рендж (§11.3) — ключевой элемент для внутридневного
сетапа: манипуляция London Open обычно снимает его границу.
"""

import numpy as np
import pandas as pd

from . import params


def killzone_of(timestamp, zones=None):
    """
    В какой killzone попадает момент времени. Возвращает имя или None.

    Границы полуинтервальные [start, end): свеча 10:00 уже вне London Open.
    """
    zones = params.KILLZONES if zones is None else zones
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    hour = ts.tz_convert('UTC').hour

    for name, (start, end) in zones.items():
        if start <= hour < end:
            return name
    return None


def killzone_mask(df, enabled=None):
    """
    Векторная версия in_killzone для всей серии свечей — нужна бэктесту,
    чтобы не звать python-функцию на каждой из сотен тысяч свечей.

    Маска считает ФАКТ попадания в сессию и намеренно НЕ смотрит на
    REQUIRE_KILLZONE: она строится один раз вместе с контекстом, а решение
    «жёсткий фильтр или просто фактор confluence» принимается позже, на
    каждой свече. Иначе параметр невозможно честно перебрать — контекст уже
    построен с зашитым в него ответом.
    """
    enabled = params.KILLZONES_ENABLED if enabled is None else enabled

    ts = pd.to_datetime(df['timestamp'], utc=True)
    hours = ts.dt.hour.to_numpy()

    mask = np.zeros(len(df), dtype=bool)
    for name in enabled:
        bounds = params.KILLZONES.get(name)
        if not bounds:
            continue
        start, end = bounds
        mask |= (hours >= start) & (hours < end)
    return mask

