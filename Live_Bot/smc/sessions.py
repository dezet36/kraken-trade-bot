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


def in_killzone(timestamp, enabled=None):
    """Разрешено ли открывать сделку в этот момент (§11.2)."""
    enabled = params.KILLZONES_ENABLED if enabled is None else enabled
    if not params.REQUIRE_KILLZONE:
        return True
    zone = killzone_of(timestamp)
    return zone is not None and zone in enabled


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


def period_opens(df, day_open_hour=None):
    """
    Открытия дня / недели / месяца (§11.1) для каждой свечи.

    Методичка: открытия старших таймфреймов используются как вспомогательные
    уровни поддержки/сопротивления, и чем старше таймфрейм открытия, тем
    сильнее уровень. На бычьем рынке ищем набор позиции НИЖЕ открытия,
    на медвежьем — ВЫШЕ.

    Возвращает DataFrame с колонками day_open / week_open / month_open.
    """
    day_open_hour = params.DAY_OPEN_HOUR_UTC if day_open_hour is None else day_open_hour

    ts = pd.to_datetime(df['timestamp'], utc=True)
    shifted = ts - pd.Timedelta(hours=day_open_hour)
    opens = pd.Series(df['open'].to_numpy(dtype=float), index=shifted)

    out = pd.DataFrame(index=df.index)
    for label, rule in (('day', 'D'), ('week', 'W'), ('month', 'MS')):
        first = opens.resample(rule).first()
        out[f'{label}_open'] = first.reindex(shifted, method='ffill').to_numpy()
    return out


def asian_range(df, at_index, day_open_hour=None):
    """
    Азиатский рендж текущего дня (§11.3) на момент свечи `at_index`.

    Используется как источник ликвидности: London Open обычно агрессивно
    снимает одну из границ рenджа, после чего идёт движение по тренду.

    Возвращает {'high','low','complete'} или None, если данных ещё нет.
    complete=False означает, что азиатская сессия ещё не закрылась.
    """
    day_open_hour = params.DAY_OPEN_HOUR_UTC if day_open_hour is None else day_open_hour
    if at_index >= len(df):
        return None

    ts = pd.to_datetime(df['timestamp'], utc=True)
    current = ts.iloc[at_index]
    day_start = (current - pd.Timedelta(hours=day_open_hour)).normalize() \
        + pd.Timedelta(hours=day_open_hour)

    asia_start, asia_end = params.KILLZONES['ASIA']
    window_start = day_start + pd.Timedelta(hours=asia_start)
    window_end = day_start + pd.Timedelta(hours=asia_end)

    # Только уже закрытые свечи: всё строго до at_index включительно
    positions = np.arange(len(df))
    mask = (
        (ts.to_numpy() >= window_start.to_datetime64())
        & (ts.to_numpy() < window_end.to_datetime64())
        & (positions <= at_index)
    )
    if not mask.any():
        return None

    highs = df['high'].to_numpy(dtype=float)[mask]
    lows = df['low'].to_numpy(dtype=float)[mask]

    return {
        'high': float(highs.max()),
        'low': float(lows.min()),
        'complete': bool(current >= window_end),
        'start': window_start,
        'end': window_end,
    }
