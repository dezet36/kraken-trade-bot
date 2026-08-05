"""
Почему в трендовых режимах не работает НИЧЕГО: проверка одной гипотезы.

Факт, который надо объяснить. Три стратегии разной природы дали одну и ту
же картину по режимам рынка:

                          рост      падение    боковик
    SMC + FIBO          -0.083      +0.079     +0.388
    пробой 96ч          -0.133      +0.054     +0.139
    пробой 336ч         -0.455      +0.093     +0.162
    пробой 336ч + 4H    -0.426      +0.136     +0.176

Возврат к уровню и пробой — противоположные ставки. Если бы дело было в
типе стратегии, знаки различались бы. Они одинаковы: все теряют в росте,
все зарабатывают в боковике. Значит, дело не в стратегии, а в самом рынке.

ГИПОТЕЗА. Режимы размечены по биткоину. Когда биткоин идёт направленно,
альткоины перестают жить своей жизнью и превращаются в его рычаг. Пять
позиций по разным парам в такой момент — не пять ставок, а одна ставка
пятикратным размером. Любая посделочная логика при этом теряет смысл:
результат определяет не сетап, а биткоин.

Если гипотеза верна, средняя корреляция пар с биткоином в трендовых
режимах должна быть заметно выше, чем в боковике. Если неверна —
объяснение надо искать другое, и market-neutral не поможет.

Меряется на часовых доходностях: корреляция каждой пары с BTC, доля
дисперсии, объяснённая биткоином, и средняя ПОПАРНАЯ корреляция альтов
между собой (она показывает, во что превращается диверсификация).

Запуск:
    python research/correlation.py
"""

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smc_market_regime import (BEAR_CACHE, BEAR_PAIRS, BULL_CACHE,  # noqa: E402
                               BULL_PAIRS, REGIMES, load_period)


def hourly_returns(period):
    """Матрица часовых доходностей: строки — время, столбцы — пары."""
    frames = {}
    for pair, data in period['data'].items():
        df = data['1h']
        ts = pd.to_datetime(df['timestamp'])
        if getattr(ts.dt, 'tz', None) is not None:
            ts = ts.dt.tz_convert('UTC').dt.tz_localize(None)
        s = pd.Series(df['close'].to_numpy(dtype=float), index=ts.to_numpy())
        frames[pair] = np.log(s).diff()
    return pd.DataFrame(frames).dropna(how='all')


def main():
    periods = [
        load_period(BULL_CACHE, BULL_PAIRS, 'бычий 2025-26'),
        load_period(BEAR_CACHE, BEAR_PAIRS, 'медвежий 2022-23'),
    ]

    for period in periods:
        rets = hourly_returns(period)
        if 'BTCUSDT' not in rets.columns:
            continue
        labels = pd.Series([period['regime'](t) for t in rets.index],
                           index=rets.index)

        print()
        print('=' * 96)
        print(f'{period["label"].upper()}   пар: {rets.shape[1]}, '
              f'часов: {len(rets)}')
        print('=' * 96)
        head = (f'{"режим":<12}{"часов":>8}{"корр. с BTC":>14}'
                f'{"доля дисперсии BTC":>21}{"попарная корр. альтов":>24}')
        print(head)
        print('-' * len(head))

        for reg in list(REGIMES) + ['всё время']:
            mask = slice(None) if reg == 'всё время' else (labels == reg).to_numpy()
            sub = rets.loc[mask].dropna(axis=1, how='all')
            sub = sub.dropna()
            if len(sub) < 100:
                continue
            btc = sub['BTCUSDT']
            alts = sub.drop(columns=['BTCUSDT'])

            corrs = alts.apply(lambda col: col.corr(btc))
            r2 = (corrs ** 2)

            # Средняя попарная корреляция альтов между собой
            cm = alts.corr().to_numpy()
            iu = np.triu_indices_from(cm, k=1)
            pairwise = float(np.nanmean(cm[iu]))

            print(f'{reg:<12}{len(sub):>8}{corrs.mean():>14.3f}'
                  f'{r2.mean() * 100:>20.1f}%{pairwise:>24.3f}')

    print()
    print('=' * 96)
    print('ЧТЕНИЕ')
    print('=' * 96)
    print('Если корреляция и доля дисперсии в росте/падении заметно выше, чем')
    print('в боковике, — гипотеза подтверждена: в тренде рынок становится')
    print('однофакторным, и портфель из пяти позиций превращается в одну')
    print('ставку на биткоин. Тогда третьей стратегии положено быть не')
    print('очередным способом выбирать сетап, а способом убрать этот фактор.')


if __name__ == '__main__':
    main()
