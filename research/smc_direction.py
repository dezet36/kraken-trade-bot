"""
Перестраивается ли система в шорт на падающем рынке — или ловит отскоки.

Вопрос конкретный и проверяемый. Разница между двумя ответами видна сразу:

  перестраивается  на падении доля шортов сильно больше половины, и растёт
                   вместе с силой падения;
  ловит отскоки    доля шортов около половины или ниже, а лонги на падении
                   не просто есть, а составляют заметную часть — и теряют.

Меряется четыре вещи:

  1. Доля лонгов и шортов в каждом режиме рынка, отдельно по периодам.
  2. Помесячно: доля шортов против фактического хода BTC за тот же месяц.
     Если система следует за рынком, эти два ряда должны идти навстречу.
  3. Результат сделок ПО тренду и ПРОТИВ него — отдельно. «Против» — это и
     есть отскоки: лонг на падающем рынке, шорт на растущем.
  4. Задержка разворота: сколько дней проходит между сменой режима рынка и
     сменой преобладающего направления сделок.

Запуск:
    python research/smc_direction.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, ci, load_period, run)

# Режим -> направление, которое ему соответствует. В боковике «своего»
# направления нет, поэтому он в разбивке «по тренду / против» не участвует.
WITH_TREND = {'рост': 'LONG', 'падение': 'SHORT'}


def block(title):
    print()
    print('=' * 96)
    print(title)
    print('=' * 96)


def shares(frames):
    block('1. ДОЛЯ НАПРАВЛЕНИЙ В КАЖДОМ РЕЖИМЕ')
    head = (f'{"период":<18}{"режим":<12}{"сделок":>8}{"лонгов":>9}{"шортов":>9}'
            f'{"R лонг":>10}{"R шорт":>10}')
    print(head)
    print('-' * len(head))
    for label, df in frames.items():
        for reg in REGIMES:
            sub = df[df.regime == reg]
            if len(sub) < 3:
                continue
            longs = sub[sub.direction == 'LONG']
            shorts = sub[sub.direction == 'SHORT']
            r_long = longs.r.mean() if len(longs) else float('nan')
            r_short = shorts.r.mean() if len(shorts) else float('nan')
            print(f'{label:<18}{reg:<12}{len(sub):>8}'
                  f'{len(longs) / len(sub) * 100:>8.0f}%'
                  f'{len(shorts) / len(sub) * 100:>8.0f}%'
                  f'{r_long:>10.3f}{r_short:>10.3f}')


def with_against(frames):
    block('3. ПО ТРЕНДУ ИЛИ ПРОТИВ НЕГО (боковик исключён — там тренда нет)')
    head = (f'{"период":<18}{"сделки":<20}{"сделок":>8}{"винрейт":>9}'
            f'{"R/сделку":>10}{"сумма R":>9}{"интервал":>24}')
    print(head)
    print('-' * len(head))
    merged_rows = []
    for label, df in frames.items():
        sub = df[df.regime.isin(WITH_TREND)].copy()
        sub['side'] = np.where(
            sub.direction == sub.regime.map(WITH_TREND), 'по тренду', 'против (отскок)')
        merged_rows.append(sub)
        for side in ('по тренду', 'против (отскок)'):
            part = sub[sub.side == side]
            if len(part) < 3:
                continue
            lo, hi = ci(part.r)
            print(f'{label:<18}{side:<20}{len(part):>8}'
                  f'{(part.r > 0).mean() * 100:>8.0f}%{part.r.mean():>10.3f}'
                  f'{part.r.sum():>9.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>24}')
    print()
    merged = pd.concat(merged_rows, ignore_index=True)
    for side in ('по тренду', 'против (отскок)'):
        part = merged[merged.side == side]
        lo, hi = ci(part.r)
        print(f'{"оба периода":<18}{side:<20}{len(part):>8}'
              f'{(part.r > 0).mean() * 100:>8.0f}%{part.r.mean():>10.3f}'
              f'{part.r.sum():>9.1f}{f"[{lo:+.3f}; {hi:+.3f}]":>24}')


def monthly(frames, periods):
    block('2. ПОМЕСЯЧНО: ДОЛЯ ШОРТОВ ПРОТИВ ФАКТИЧЕСКОГО ХОДА BTC')
    print('Если система следует за рынком, столбцы должны идти навстречу:')
    print('месяц с падением BTC — высокая доля шортов.')
    for period in periods:
        label = period['label']
        df = frames.get(label)
        if df is None:
            continue
        # Время лежит в колонке timestamp, а не в индексе: без этого resample
        # молча собирал пустые группы и вся колонка BTC печаталась как nan.
        btc = period['data']['BTCUSDT']['1d'].copy()
        btc['timestamp'] = pd.to_datetime(btc['timestamp'])
        if btc['timestamp'].dt.tz is not None:
            btc['timestamp'] = btc['timestamp'].dt.tz_convert('UTC').dt.tz_localize(None)
        closes = btc.set_index('timestamp')['close']
        monthly_ret = closes.resample('MS').last() / closes.resample('MS').first() - 1
        print()
        print(label)
        print(f'   {"месяц":<9}{"BTC":>8}{"сделок":>8}{"шортов":>9}   что делает бот')
        grouped = df.set_index('entry_time').resample('MS')
        for ts, sub in grouped:
            if not len(sub):
                continue
            share = (sub.direction == 'SHORT').mean()
            move = monthly_ret.get(ts, float('nan'))
            arrow = '▼' if move < -0.02 else ('▲' if move > 0.02 else '·')
            bar = '█' * int(round(share * 20))
            print(f'   {ts:%Y-%m}  {move * 100:>+6.1f}% {arrow}{len(sub):>7}'
                  f'{share * 100:>8.0f}%   {bar}')
        # Согласованность: падал BTC — росла ли доля шортов
        shares_by_month, moves = [], []
        for ts, sub in grouped:
            if len(sub) < 5 or ts not in monthly_ret.index:
                continue
            shares_by_month.append((sub.direction == 'SHORT').mean())
            moves.append(monthly_ret[ts])
        if len(shares_by_month) >= 4:
            corr = np.corrcoef(shares_by_month, moves)[0, 1]
            print(f'   связь «ход BTC ↔ доля шортов»: {corr:+.2f} '
                  f'(-1 = идеально следует за рынком, 0 = не реагирует)')


def switch_lag(frames, periods):
    block('4. ЗАДЕРЖКА РАЗВОРОТА')
    print('Скользящее окно 20 сделок: доля шортов против режима рынка в день входа.')
    for period in periods:
        label = period['label']
        df = frames.get(label)
        if df is None or len(df) < 40:
            continue
        d = df.sort_values('entry_time').reset_index(drop=True)
        d['short_roll'] = (d.direction == 'SHORT').rolling(20).mean()
        print()
        print(label)
        for reg in ('рост', 'падение', 'боковик'):
            sub = d[(d.regime == reg) & d.short_roll.notna()]
            if len(sub) < 5:
                continue
            print(f'   {reg:<10} доля шортов в окне: медиана '
                  f'{sub.short_roll.median() * 100:>3.0f}%   '
                  f'диапазон {sub.short_roll.min() * 100:.0f}–'
                  f'{sub.short_roll.max() * 100:.0f}%')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]
    frames = {}
    for period in periods:
        stats = run(period)
        if stats is None:
            continue
        df = stats['rows'].dropna(subset=['regime']).copy()
        df['entry_time'] = pd.to_datetime(df['entry_time'])
        frames[period['label']] = df
        print(f'   [{period["label"]}] {len(df)} сделок', flush=True)

    shares(frames)
    monthly(frames, periods)
    with_against(frames)
    switch_lag(frames, periods)

    block('ОТВЕТ')
    print('Читается по пункту 2 (связь с ходом BTC) и пункту 3 (по тренду')
    print('против отскоков). Отрицательная связь около -0.5 и ниже означает,')
    print('что система разворачивается вслед за рынком. Убыточная строка')
    print('«против (отскок)» при заметном числе сделок означает, что часть')
    print('оборота уходит на ловлю отскоков — и это чинится порогом, а не')
    print('запретом направления.')


if __name__ == '__main__':
    main()
