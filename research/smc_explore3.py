"""
SMC, разведка — третий шаг (30.09.2026): фильтр толпы и волатильность на 23
НОВЫХ парах (кэш 12m вне списка бота; в подборе ни одного правила не участвовали).

Фандинг этих пар скачан в отдельную копию кэша research/backtest_cache_12mx
(общий backtest_cache_12m не трогаем: чужие замеры читают его funding/).
HFT с биржи снят — ставки нет, фильтр её пропускает, как бот. Ставка
приведена к 8 ч (у 11 пар выплата раз в 4 ч — см. _interval_scale).

Запуск:
    python research/smc_explore3.py > research/results/smc_explore3.txt
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_doctrine as D                               # noqa: E402

D.PERIODS['12m'] = 'backtest_cache_12mx'              # только 23 новые пары + BTC

import pandas as pd                                   # noqa: E402

import smc_improve as I                               # noqa: E402
from common import ci                                 # noqa: E402
from smc_explore2 import run, vol                      # noqa: E402

E = I.E
FT = 0.0005


def _interval_scale():
    """
    ИНТЕРВАЛ ВЫПЛАТ. У 11 из 23 новых пар фандинг платится раз в 4 ч, у пар
    списка бота — раз в 8 ч. Ставка за выплату у первых вдвое меньше, и порог
    ±1 б.п. значил бы для них другое. Ставка приводится к 8 ч: × 8 / интервал.
    """
    out = {}
    folder = os.path.join(HERE, D.PERIODS['12m'], 'funding')
    for name in os.listdir(folder):
        ts = pd.to_datetime(pd.read_csv(os.path.join(folder, name))['timestamp'], utc=True).sort_values()
        hours = float(ts.diff().dt.total_seconds().dropna().median() / 3600)
        out[name[:-4]] = 8.0 / hours if hours > 0 else 1.0
    return out


SCALE = _interval_scale()


def crowd(r, x):
    rate = (x or {}).get('funding', np.nan) * SCALE.get(r.pair, 1.0)
    return not (rate > -1.0)


def main():
    x12 = tuple(sorted(set(E.rows('12m', 'live12x')['pair'])))
    print(f'23 НОВЫЕ ПАРЫ, 12m (2025-05 … 2026-07), портфель «как бот»')
    for filt, label in ((None, 'база (без фандинга)'), (crowd, 'фильтр толпы (как бот)'),
                        (vol(75), 'волатильность ≥ 75'),
                        (lambda r, x: vol(75)(r, x) and crowd(r, x), 'волатильность ≥ 75 и толпа'),
                        (lambda r, x: vol(50)(r, x) and crowd(r, x), 'волатильность ≥ 50 и толпа')):
        run('live12x', x12, ('12m',), filt, label)
        run('live12x', x12, ('12m',), filt, '   … насквозь', FT)
    print('\nКАЖДАЯ ЗАЯВКА ОТДЕЛЬНО (касанием)')
    frame = I.build(('12m',), 'live12x', x12)
    fund = pd.Series([(x or {}).get('funding', np.nan) * SCALE.get(p, 1.0)
                      for x, p in zip(frame['feats'], frame['pair'])], index=frame.index)
    atr = frame['feats'].map(lambda x: (x or {}).get('atr_rank', np.nan))
    have = fund.notna()
    print(f'  заявок {len(frame)}, со ставкой {int(have.sum())}')
    for label, mask in (('все', frame['r'].notna()),
                        ('толпа пропускает', ~(fund > -1.0)),
                        ('толпа отсекает', fund > -1.0),
                        ('волатильность ≥ 75', atr >= 75),
                        ('волатильность ≥ 75 и толпа', (atr >= 75) & ~(fund > -1.0))):
        r = frame.loc[mask & frame['r'].notna(), 'r'].to_numpy(dtype=float)
        lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
        print(f'  {label:30s} {len(r):4d} сд, в плюс {np.mean(r > 0):4.0%}, {r.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
