"""
П-21 (docs/Независимый_разбор_2026-10-01.md, раздел 5): живая SMC на 2021 годе.

ЗАЧЕМ. Весь плюс SMC — фильтр толпы, выбранный и принятый на 2022–2026. На
2021 году мерили только межтаймфреймовый вариант (smcr_mtf_2021.py), живую
SMC — ни разу. Это единственная проверка вне выборки, возможная на истории.

ЗАПИСАНО ДО ПРОГОНА (01.10.2026 около 20:00 UTC, до скачивания данных):
живая SMC — ядро как в боте, фильтр толпы −1 б.п., 10 пар пула, правила
брокера, фандинг по факту, стенд «глазами бота» (smc_lab_live + smc_lab_eval,
без правок). Данные — Binance UM 2020-09…2022-01 (у Bybit в 2021 полный год
только BTC, LINK, LTC), проверка — срез Bybit, где пара уже торговалась.
Фильтр «держится», если с ним R на сделку > 0 И лучше, чем без него.
Прогноз: ставка в 2021 почти всегда выше +1 б.п. → «только шорты в бычьем
году» → минус.

ИТОГ: Binance — без фильтра 180 сд −0.156R, с фильтром 71 сд −0.312R
[−0.72; +0.17], 67 из 71 — шорты; Bybit — без фильтра 127 сд −0.220R, с
фильтром 60 сд −0.471R [−0.86; +0.01]. Не держится. 2021 (Binance) и 2022–26
вместе: 249 сд +0.279R [−0.02; +0.59].

    python research/smcr_y21_fetch.py binance|bybit   # кэши backtest_cache_y21b / y21y
    python research/smcr_y21.py check                 # сверка драйвера: 2022–26 → 178 сд +91.7R
    python research/smcr_y21.py gen y21b|y21y         # сетапы ядра «глазами бота» (~40 мин на 4 ядрах)
    python research/smcr_y21.py eval                  # → results/smcr/eval_y21.txt
    python research/smcr_y21.py combo                 # 2021 и 2022–26 одной выборкой
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

import logger                                          # noqa: E402
logger.log = lambda *a, **k: None

import ai_doctrine as D                               # noqa: E402

OUT = os.path.join(HERE, 'results')
D.PERIODS.update({'y21b': 'backtest_cache_y21b', 'y21y': 'backtest_cache_y21y'})
SOURCES = (('y21b', 'Binance'), ('y21y', 'Bybit'))
START = int(pd.Timestamp('2021-01-01', tz='UTC').value // 10 ** 6)
END = int(pd.Timestamp('2022-01-01', tz='UTC').value // 10 ** 6)


def crowd_ok(r, f):
    """Фильтр толпы бота: ставка в сторону сделки ≤ −1 б.п.; нет ставки — не отказ."""
    return not ((f or {}).get('funding', np.nan) > -1.0)


def gen(period):
    """Сетапы на окне бота за 2020-09…2022-01; в файл — только решения 2021 года."""
    import smc_lab_live as LL
    LL.generate([period])
    path = LL.rows_path(period)
    f = pd.read_pickle(path)
    f = f[(f['t'] >= START) & (f['t'] < END)].reset_index(drop=True)
    f.to_pickle(path + '.tmp')
    os.replace(path + '.tmp', path)
    print(f'{period}: в 2021 — {len(f)} срабатываний, пар {f["pair"].nunique()}')


def _E():
    import smc_lab_eval as E
    E.BAR_H.setdefault('live', 1.0)
    return E


def _line(label, rs):
    from common import ci
    r = np.array(rs, dtype=float)
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    return (f'  {label:46s} {len(r):4d} сд {r.sum():+7.1f}R {r.mean() if len(r) else 0:+.3f} '
            f'[{lo:+.3f}; {hi:+.3f}]  в плюс {np.mean(r > 0) * 100 if len(r) else 0:3.0f}%')


def check():
    E = _E()
    E.run({'layouts': ('live',), 'filt': crowd_ok}, 'живая SMC 2022–26, без двойного счёта')
    E.run({'layouts': ('live',), 'filt': None}, 'то же без фильтра толпы')


def evaluate():
    E = _E()
    pool = tuple(E.POOL10)
    out, rows = [], []
    for period, name in SOURCES:
        if not os.path.exists(os.path.join(OUT, f'smc_lab_rows_{period}_live.pkl')):
            continue
        out.append(f'\n2021, свечи и фандинг {name}, портфель с правилами брокера SMC, R на сделку')
        for crowd in (False, True):
            spec = dict(E.LIVE, pairs=pool, layouts=('live',), filt=crowd_ok if crowd else None)
            for ft in (0.0, 0.0005):
                res, _ = E.portfolio(period, dict(spec, fill_through=ft))
                tag = ('с фильтром толпы (как в боте)' if crowd else 'без фильтра (ядро)') + \
                      (', насквозь 0.05%' if ft else '')
                out.append(_line(tag, [t['pnl'] / t['risk'] for t in res['trades']]))
                if ft == 0.0:
                    rows += [{'src': name, 'crowd': crowd, 'pair': t['pair'], 'dir': t['direction'],
                              'r': t['pnl'] / t['risk'], 'entry_time': t['entry_time']} for t in res['trades']]
        orders = E.orders_for(period, dict(E.LIVE, pairs=pool, layouts=('live',), filt=None))
        fund = np.array([(o.meta['row'].dir,
                          (E.features(period, o.pair, int(o.meta['row'].t), int(o.meta['row'].dir), float(o.entry),
                                      float(o.meta['row'].stop), float(o.meta['row'].targets[0])) or {})
                          .get('funding', np.nan)) for o in orders], dtype=float)
        for side, nm in ((1, 'лонги'), (-1, 'шорты')):
            x = fund[fund[:, 0] == side, 1]
            ok = ~(x > -1.0)
            out.append(f'  заявки {nm}: {len(x):4d}, фильтр пропускает {ok.sum():4d} '
                       f'({ok.mean() * 100 if len(x) else 0:.0f}%), ставка в сторону сделки: медиана '
                       f'{np.nanmedian(x) if len(x) else np.nan:+.2f} б.п.')
    f = pd.DataFrame(rows)
    if len(f):
        f.to_pickle(os.path.join(OUT, 'smcr', 'y21_trades.pkl'))
        f['q'] = pd.to_datetime(f['entry_time']).dt.to_period('Q').astype(str)
        out.append('\nразбивка (налив касанием): источник, фильтр, сторона, квартал входа')
        for (src, crowd, d, q), g in f.groupby(['src', 'crowd', 'dir', 'q']):
            out.append(f'  {src:8s} {"фильтр" if crowd else "ядро  "} {str(d):8s} {q}  '
                       f'{len(g):3d} сд {g.r.sum():+6.1f}R')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(OUT, 'smcr', 'eval_y21.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')


def combo():
    """2022–26 («глазами бота», без двойного счёта) + 2021 Binance одной выборкой."""
    from common import ci
    E = _E()
    a = E.run({'layouts': ('live',), 'filt': crowd_ok}, '2022–26', quiet=True)['r']
    f = pd.read_pickle(os.path.join(OUT, 'smcr', 'y21_trades.pkl'))
    b = f[(f.src == 'Binance') & f.crowd].r.to_numpy(float)
    for label, r in (('2022–26', a), ('2021 (Binance)', b), ('2021–26 вместе', np.concatenate([a, b]))):
        lo, hi = ci(r)
        print(f'  {label:16s} {len(r):4d} сд {r.sum():+7.1f}R {r.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    cmd = sys.argv[1]
    if cmd == 'gen':
        gen(sys.argv[2])
    else:
        {'check': check, 'eval': evaluate, 'combo': combo}[cmd]()
