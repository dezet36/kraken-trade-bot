"""
Чего ждать за месяц: распределение, а не одно число.

Считать «средний R × число сделок» бессмысленно. При винрейте около 33% и
редких дальних целях месяц определяется тем, попали в него две крупные сделки
или ни одной. Ответ на вопрос «сколько будет за месяц» — это разброс, и
показывать надо именно его.

Метод: скользящее окно 30 дней по фактической истории (2.5 года, три режима
рынка), шаг — сутки. В каждом окне считается результат по сделкам, ЗАКРЫТЫМ в
нём. Так сохраняется реальная кучность: сделки идут пачками, и месяц с пятью
сделками подряд — обычное дело, чего не даст ни одна выборка «наугад».

Оговорка, которую нельзя опускать: соседние окна перекрываются и не являются
независимыми. Проценты описывают, насколько РАЗНЫМИ бывали месяцы в прошлом,
а не строгий доверительный интервал на будущее. Для сверки печатается и
разбивка по непересекающимся месяцам.

Запуск:
    python research/smc_forecast.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, load_period, run)

WINDOW_DAYS = 30
RISK_LEVELS = (0.5, 1.0)     # % депозита на сделку


def windows(df, days=WINDOW_DAYS):
    """Скользящие окна по времени ЗАКРЫТИЯ сделок."""
    if df.empty:
        return []
    df = df.sort_values('close_time')
    start = df.close_time.min()
    end = df.close_time.max() - pd.Timedelta(days=days)
    if end <= start:
        return []

    out = []
    day = start
    while day <= end:
        stop = day + pd.Timedelta(days=days)
        sub = df[(df.close_time >= day) & (df.close_time < stop)]
        if len(sub):
            out.append({
                'from': day,
                'trades': len(sub),
                'sum_r': sub.r.sum(),
                'regime': sub.regime.mode().iloc[0] if len(sub.regime.mode()) else '—',
                'worst_r': drawdown_r(sub.r.to_numpy()),
            })
        day += pd.Timedelta(days=1)
    return out


def drawdown_r(values):
    """Максимальная просадка накопленной кривой внутри окна, в R."""
    curve = np.cumsum(values)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))
    return float(np.max(peak - np.concatenate([[0.0], curve])))


def show(rows, title):
    if not rows:
        print(f'{title}: данных нет')
        return
    frame = pd.DataFrame(rows)
    print()
    print('=' * 96)
    print(title)
    print('=' * 96)

    q = frame.sum_r.quantile
    print(f'Окон: {len(frame)}   сделок в месяц: медиана {frame.trades.median():.0f} '
          f'(от {frame.trades.min()} до {frame.trades.max()})')
    print()
    head = (f'{"итог месяца":<22}{"в R":>10}' +
            ''.join(f'{f"при риске {r}%":>16}' for r in RISK_LEVELS))
    print(head)
    print('-' * len(head))

    def line(label, value):
        cells = ''.join(f'{value * r:>+15.1f}%' for r in RISK_LEVELS)
        print(f'{label:<22}{value:>+10.1f}{cells}')

    line('худший из окон', frame.sum_r.min())
    line('плохой (10%)', q(0.10))
    line('нижняя четверть', q(0.25))
    line('медиана', frame.sum_r.median())
    line('верхняя четверть', q(0.75))
    line('хороший (90%)', q(0.90))
    line('лучший из окон', frame.sum_r.max())

    losing = (frame.sum_r < 0).mean() * 100
    print()
    print(f'Убыточных месяцев: {losing:.0f}%   '
          f'просадка внутри месяца: медиана {frame.worst_r.median():.1f}R, '
          f'худшая {frame.worst_r.max():.1f}R')

    print()
    print('По режиму рынка в окне:')
    sub_head = f'{"режим":<12}{"окон":>7}{"медиана R":>12}{"10%":>9}{"90%":>9}{"убыточных":>12}'
    print(sub_head)
    print('-' * len(sub_head))
    for name in REGIMES:
        sub = frame[frame.regime == name]
        if len(sub) < 5:
            continue
        print(f'{name:<12}{len(sub):>7}{sub.sum_r.median():>+12.1f}'
              f'{sub.sum_r.quantile(0.10):>+9.1f}{sub.sum_r.quantile(0.90):>+9.1f}'
              f'{(sub.sum_r < 0).mean() * 100:>11.0f}%')


def calendar_months(df):
    """Непересекающиеся календарные месяцы — для сверки со скользящим окном."""
    if df.empty:
        return
    frame = df.assign(month=df.close_time.dt.to_period('M'))
    grouped = frame.groupby('month').r.agg(['count', 'sum'])
    print()
    print('Непересекающиеся календарные месяцы (для сверки):')
    print(f'   месяцев: {len(grouped)}   в плюс: {(grouped["sum"] > 0).sum()}   '
          f'в минус: {(grouped["sum"] <= 0).sum()}')
    print(f'   медиана {grouped["sum"].median():+.1f}R   '
          f'худший {grouped["sum"].min():+.1f}R   лучший {grouped["sum"].max():+.1f}R')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]

    frames = []
    for period in periods:
        stats = run(period)
        if stats is None:
            continue
        df = stats['rows'].dropna(subset=['regime']).copy()
        df['close_time'] = df.entry_time + pd.to_timedelta(df.days, unit='D')
        frames.append(df.assign(period=period['label']))
        show(windows(df), f'{period["label"].upper()}: месяц по скользящему окну')

    if not frames:
        return
    both = pd.concat(frames, ignore_index=True)
    rows = []
    for frame in frames:
        rows += windows(frame)
    show(rows, 'ОБА ПЕРИОДА: чего ждать от произвольного месяца')
    calendar_months(both)

    print()
    print('Как читать. «В R» не зависит от депозита и размера риска: 1R — это')
    print('сумма, которой рискуем в одной сделке. Столбцы справа переводят её в')
    print('проценты депозита при разных настройках риска.')
    print()
    print('Чего эти числа НЕ учитывают: живой рынок отличается от истории,')
    print('пул пар в боте меньше, чем в бэктесте, и месяц — слишком короткий')
    print('срок, чтобы отличить умение от везения.')


if __name__ == '__main__':
    main()
