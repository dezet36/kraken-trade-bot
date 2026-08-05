"""
Трендовый пробой: генератор ордеров и замер на наших данных.

ЗАЧЕМ. Обе действующие стратегии ставят на одно и то же — что цена вернётся
к уровню. FIBO ждёт отката в сетку Фибоначчи, SMC ждёт возврата в
ордер-блок. Замер показал, чем это оборачивается:

    трендовые режимы (рост + падение):   252 сделки    +5.6 R
    боковик:                             580 сделок  +224.9 R

В выраженном тренде цена до зоны не доходит, и обе простаивают. Пробойная
стратегия ставит на противоположное — на продолжение движения, — и потому
зарабатывать должна ровно там, где эти две пусты.

Никакая стратегия не работает во всех режимах. Работать во всех режимах
может ПОРТФЕЛЬ, собранный из стратегий с разными режимами силы. Именно это
здесь и проверяется: не «хороша ли пробойная сама по себе», а «добавляет
ли она то, чего нет».

ПРАВИЛА (классика, без изобретений — чтобы не подгонять):

    вход    закрытие 1H выше максимума N предыдущих баров (лонг) или ниже
            минимума N баров (шорт); ордер стоп-типа, налив по факту ухода
            цены за уровень
    стоп    K x ATR(14) от цены входа
    ведение трейлинг на дистанции T x ATR; фиксированных целей нет —
            прибыль трендовой стратегии живёт в редких длинных движениях,
            и цель обрезает ровно тот хвост, ради которого всё затевается
    фильтр   опционально: торговать только в направлении тренда старшего ТФ

ЧЕСТНОСТЬ. Уровень канала и ATR считаются по барам ДО сигнальной свечи
включительно, ордер создаётся в момент её ЗАКРЫТИЯ. Движок исполняет
стоп-вход по открытию свечи при разрыве через уровень и берёт тейкерскую
комиссию. Всё это добавлено в smc_engine специально для этого замера.

Запуск:
    python research/breakout.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_engine import Order, compute_stats, run_portfolio  # noqa: E402
from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period)


def atr(df, period=14):
    """Средний истинный диапазон. Значение на баре i считается по барам <= i."""
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    out = np.full(len(tr), np.nan)
    if len(tr) > period:
        # Уайлдеровское сглаживание
        out[period] = tr[1:period + 1].mean()
        for i in range(period + 1, len(tr)):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def htf_direction(htf_df, fast=50, slow=200):
    """
    Направление старшего ТФ по двум скользящим: время -> +1/-1/0.

    Значение на баре считается по барам ДО него включительно и применяется
    к сигналам ПОСЛЕ его закрытия — иначе фильтр знал бы будущее.
    """
    ts = pd.to_datetime(htf_df['timestamp'])
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    close = htf_df['close']
    ema_f = close.ewm(span=fast, adjust=False).mean().to_numpy()
    ema_s = close.ewm(span=slow, adjust=False).mean().to_numpy()
    sign = np.where(ema_f > ema_s, 1, -1)
    sign[:slow] = 0
    times = ts.to_numpy(dtype='datetime64[ns]')
    bar = int(np.median(np.diff(times).astype('int64'))) if len(times) > 2 else 0
    closes_at = times + np.timedelta64(bar, 'ns')

    def lookup(when):
        idx = int(np.searchsorted(closes_at, when, 'right')) - 1
        return int(sign[idx]) if 0 <= idx < len(sign) else 0
    return lookup


def build_orders(pair, df, channel=48, atr_stop=2.0, atr_trail=3.0,
                 atr_period=14, expiry_hours=6.0, htf_lookup=None):
    """
    Ордера пробоя для одной пары.

    channel      сколько баров назад берётся экстремум канала
    atr_stop     дистанция стопа в ATR
    atr_trail    дистанция трейлинга в ATR
    expiry_hours сколько ждать ухода за уровень; пробой — событие срочное,
                 через сутки уровень уже неактуален
    htf_close    закрытия старшего ТФ для фильтра направления (None — без него)
    """
    ts = pd.to_datetime(df['timestamp'])
    if getattr(ts.dt, 'tz', None) is not None:
        ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
    ts = ts.to_numpy(dtype='datetime64[ns]')
    high = df['high'].to_numpy(dtype=float)
    low = df['low'].to_numpy(dtype=float)
    close = df['close'].to_numpy(dtype=float)
    a = atr(df, atr_period)

    bar_ns = int(np.median(np.diff(ts).astype('int64'))) if len(ts) > 2 else 0
    expiry = np.timedelta64(int(expiry_hours * 3600), 's')

    orders = []
    last_side = {}
    for i in range(max(channel, atr_period) + 1, len(df)):
        if np.isnan(a[i]):
            continue
        # Канал ТОЛЬКО по предыдущим барам: включив текущий, мы бы сравнивали
        # закрытие с максимумом, в который это же закрытие и входит.
        hh = high[i - channel:i].max()
        ll = low[i - channel:i].min()

        if close[i] > hh:
            side = 'LONG'
        elif close[i] < ll:
            side = 'SHORT'
        else:
            continue

        if htf_lookup is not None:
            want = 1 if side == 'LONG' else -1
            if htf_lookup(ts[i]) != want:
                continue

        # Один пробой одного канала торгуется один раз: пока цена держится
        # выше уровня, каждая следующая свеча формально тоже «пробой».
        if last_side.get(pair) == side and orders and \
                (ts[i] - orders[-1].created) < np.timedelta64(int(channel * bar_ns)):
            continue
        last_side[pair] = side

        created = ts[i] + np.timedelta64(bar_ns, 'ns')
        entry = close[i]
        dist = atr_stop * a[i]
        stop = entry - dist if side == 'LONG' else entry + dist
        # Целей нет: выход только по трейлингу или тайм-стопу. Доля 1.0 на
        # недостижимом уровне — способ сказать движку «не фиксируй частями».
        far = entry + dist * 1000 if side == 'LONG' else entry - dist * 1000

        orders.append(Order(
            pair=pair, direction=side, entry=float(entry), stop=float(stop),
            targets=[float(far)], fractions=[1.0],
            created=created, expires=created + expiry,
            key=(pair, 'BRK', int(i // max(channel, 1)), side),
            entry_type='stop',
            trail_distance=float(atr_trail * a[i]),
            meta={'atr': float(a[i]), 'channel': channel},
        ))
    return orders


def build_for_period(period, htf=None, **kwargs):
    orders = []
    for pair, data in period['data'].items():
        lookup = htf_direction(data[htf]) if htf else None
        orders += build_orders(pair, data['1h'], htf_lookup=lookup, **kwargs)
    return orders


def run(period, orders):
    from smc import params as P
    bt = period['bt']
    result = run_portfolio(
        orders, {p: period['data'][p]['5m'] for p in period['data']},
        risk_pct=bt.RISK_PCT, max_positions=bt.MAX_POSITIONS,
        cooldown_hours=bt.COOLDOWN_HOURS,
        max_same_direction=P.MAX_SAME_DIRECTION,
        breakeven_after_tp1=False)
    if not result['trades']:
        return None
    stats = compute_stats(result, label='')
    rows = []
    for t in result['trades']:
        if not t.get('risk'):
            continue
        rows.append({
            'r': t['pnl'] / t['risk'],
            'regime': period['regime'](t['entry_time']),
            'direction': 'LONG' if t['direction'] in ('BULLISH', 'LONG') else 'SHORT',
            'entry_time': pd.Timestamp(t['entry_time']),
            'days': (pd.Timestamp(t['exit_time']) - pd.Timestamp(t['entry_time'])
                     ).total_seconds() / 86400,
        })
    stats['rows'] = pd.DataFrame(rows)
    return stats


# Первый прогон дал монотонную картину: чем длиннее канал, тем лучше, и
# только 168 часов (7 дней) вышел в плюс на обоих периодах. Это указывает
# туда, откуда трендследование родом: классика Дончиана — 20 и 55 ДНЕЙ, а не
# часов. Продолжаем шкалу до 30 дней, иначе замер обрывается ровно там, где
# начинает работать. Заодно проверяем фильтр направления по 4H и по 1D:
# «пробой только по тренду старшего ТФ» — стандартное лекарство от ложных
# пробоев в боковике.
CONFIGS = [
    ('канал 96 (4д), стоп 3, трейл 5',   dict(channel=96, atr_stop=3.0, atr_trail=5.0)),
    ('канал 168 (7д), стоп 3, трейл 5',  dict(channel=168, atr_stop=3.0, atr_trail=5.0)),
    ('канал 336 (14д), стоп 3, трейл 5', dict(channel=336, atr_stop=3.0, atr_trail=5.0)),
    ('канал 480 (20д), стоп 3, трейл 5', dict(channel=480, atr_stop=3.0, atr_trail=5.0)),
    ('канал 720 (30д), стоп 3, трейл 5', dict(channel=720, atr_stop=3.0, atr_trail=5.0)),
    ('канал 480, трейл 8',               dict(channel=480, atr_stop=3.0, atr_trail=8.0)),
    ('канал 336 + фильтр 4H',            dict(channel=336, atr_stop=3.0, atr_trail=5.0,
                                              htf='4h')),
    ('канал 336 + фильтр 1D',            dict(channel=336, atr_stop=3.0, atr_trail=5.0,
                                              htf='1d')),
]


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    results = {}
    for name, kw in CONFIGS:
        for period in periods:
            orders = build_for_period(period, **kw)
            stats = run(period, orders)
            if stats is None:
                print(f'   [{period["label"]}] {name}: сделок нет')
                continue
            results[(period['label'], name)] = stats
            df = stats['rows']
            print(f'   [{period["label"]}] {name}: ордеров {len(orders)}, '
                  f'{len(df)} сделок, {stats["return_pct"]:+.1f}%, '
                  f'DD {stats["max_dd_pct"]:.1f}%, сумма R {df.r.sum():+.1f}',
                  flush=True)

    for period in periods:
        label = period['label']
        print()
        print('=' * 104)
        print(label.upper())
        print('=' * 104)
        head = (f'{"конфигурация":<32}{"сделок":>8}{"винрейт":>9}{"R/сделку":>10}'
                f'{"сумма R":>9}{"доход%":>9}{"DD%":>7}{"дней":>7}')
        print(head)
        print('-' * len(head))
        for name, _ in CONFIGS:
            stats = results.get((label, name))
            if not stats:
                continue
            df = stats['rows']
            print(f'{name:<32}{len(df):>8}{(df.r > 0).mean() * 100:>8.0f}%'
                  f'{df.r.mean():>10.3f}{df.r.sum():>9.1f}'
                  f'{stats["return_pct"]:>+9.1f}{stats["max_dd_pct"]:>7.1f}'
                  f'{df.days.median():>7.1f}')

    print()
    print('=' * 104)
    print('ГЛАВНОЕ: ЗАРАБАТЫВАЕТ ЛИ ОНА ТАМ, ГДЕ ПУСТО У НАС')
    print('=' * 104)
    print('Для сравнения — текущий портфель: рост -0.083, падение +0.079, '
          'боковик +0.388')
    print()
    head = f'{"конфигурация":<32}' + ''.join(f'{r:>26}' for r in REGIMES)
    print(head)
    print('-' * len(head))
    for name, _ in CONFIGS:
        frames = [results[(p['label'], name)]['rows'] for p in periods
                  if (p['label'], name) in results]
        if not frames:
            continue
        merged = pd.concat(frames, ignore_index=True)
        parts = []
        for reg in REGIMES:
            sub = merged[merged.regime == reg]
            if len(sub) < 3:
                parts.append(f'{"—":>26}')
                continue
            lo, hi = ci(sub.r)
            parts.append(f'{sub.r.mean():>8.3f} [{lo:+.2f};{hi:+.2f}]'.rjust(26))
        print(f'{name:<32}' + ''.join(parts))

    print()
    print('Стратегия принимается в портфель НЕ за собственную доходность, а')
    print('за то, что её сильные режимы не совпадают с нашими. Если она тоже')
    print('зарабатывает только в боковике — это третья копия того, что есть.')


if __name__ == '__main__':
    main()
