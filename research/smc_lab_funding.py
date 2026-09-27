"""
Проверка устойчивости единственной принятой гипотезы стенда — H6a «против
толпы»: SMC берёт сетап, только если фандинг НЕ в пользу толпы в сторону
сделки (для лонга ставка ≤ 0 — шорты платят лонгам; для шорта ≥ 0).

ПОСЛЕ ПРОСМОТРА (27.09.2026): H6a прошла приёмку одна из 21, а при 21 проверке
одна такая может выйти случайно. Здесь — то, что должно выполняться, если
эффект настоящий, и что НЕ подбирается:
    1. доза: чем сильнее толпа против сделки, тем лучше (пороги ставки);
    2. обратная сторона (толпа за сделку) хуже базы;
    3. независимая выборка пар — 10 пар вне пула SMC;
    4. половинки каждого периода — 10 кусков вместо 5;
    5. лонги и шорты отдельно;
    6. порядок одновременных заявок (живой и «как в старом бэктесте»);
    7. каждая заявка отдельно, без портфеля — R по знаку фандинга с интервалом.

Запуск (после research/smc_lab.py gen):
    python research/smc_lab_funding.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_lab_eval as E                              # noqa: E402
from common import ci                                 # noqa: E402

OTHER10 = tuple(p for p in E.ALL20 if p not in E.POOL10)


def fund(f):
    return f.get('funding', np.nan)


def against(limit):
    return lambda r, f: fund(f) <= limit


def halves(spec, label):
    """Итог портфеля по половинкам каждого периода (по времени закрытия сделки)."""
    spec = dict(E.LIVE, **spec)
    cells = []
    for period in E.PERIODS:
        res, orders = E.portfolio(period, spec)
        trades = res['trades']
        if not trades:
            cells += [0.0, 0.0]
            continue
        times = np.array([np.datetime64(t['entry_time']).astype('datetime64[s]').astype('int64') for t in trades])
        mid = np.median(times)
        rs = np.array([t['pnl'] / t['risk'] for t in trades])
        cells += [rs[times <= mid].sum(), rs[times > mid].sum()]
    return cells


def main():
    print('1. ДОЗА: порог ставки фандинга (б.п. за 8 ч, знак — в сторону сделки)')
    base = E.run({}, 'база: живая SMC')
    for limit in (1.0, 0.5, 0.0, -0.5, -1.0):
        E.run({'filt': against(limit)}, f'толпа против: фандинг ≤ {limit:+.1f} б.п.')
    E.run({'filt': lambda r, f: f.get('funding_3', np.nan) <= 0}, 'среднее за 3 выплаты ≤ 0')

    print('\n2. ОБРАТНАЯ СТОРОНА: толпа за сделку (фандинг > 0)')
    E.run({'filt': lambda r, f: fund(f) > 0}, 'толпа за сделку')

    print('\n3. ДРУГИЕ 10 ПАР (вне пула SMC — независимая выборка)')
    E.run({'pairs': OTHER10}, 'другие 10 пар, без фильтра')
    E.run({'pairs': OTHER10, 'filt': against(0.0)}, 'другие 10 пар, против толпы')
    E.run({'pairs': E.ALL20}, 'все 20 пар, без фильтра')
    E.run({'pairs': E.ALL20, 'filt': against(0.0)}, 'все 20 пар, против толпы')

    print('\n4. ПОЛОВИНКИ ПЕРИОДОВ (итог в R; 10 кусков)')
    for label, spec in (('база', {}), ('против толпы', {'filt': against(0.0)})):
        cells = halves(spec, label)
        print(f'  {label:14s} ' + ' '.join(f'{c:+6.1f}' for c in cells) + f'   в плюс {sum(c > 0 for c in cells)} из 10')

    print('\n5. ЛОНГИ И ШОРТЫ')
    for side, name in ((1, 'лонги'), (-1, 'шорты')):
        E.run({'filt': lambda r, f, s=side: r.dir == s}, f'{name}, без фильтра')
        E.run({'filt': lambda r, f, s=side: r.dir == s and fund(f) <= 0}, f'{name}, против толпы')

    print('\n6. ПОРЯДОК ОДНОВРЕМЕННЫХ ЗАЯВОК «как в старом бэктесте»')
    pool = {'tiebreak': 'pool', 'pairs': tuple(__import__('llm_rules').POOL)}
    E.run(pool, 'база')
    E.run(dict(pool, filt=against(0.0)), 'против толпы')

    print('\n7. КАЖДАЯ ЗАЯВКА ОТДЕЛЬНО: R по знаку фандинга (живые пороги, 10 пар)')
    spec = dict(E.LIVE, filt=lambda r, f: True)      # фильтр-пустышка — чтобы посчитать признаки
    groups = {'толпа против (≤ 0)': [], 'толпа за (> 0)': []}
    per = {k: {} for k in groups}
    for period in E.PERIODS:
        for o, r in E.per_setup(period, spec):
            if r is None or o.meta['feats'] is None:
                continue
            k = 'толпа против (≤ 0)' if fund(o.meta['feats']) <= 0 else 'толпа за (> 0)'
            groups[k].append(r)
            per[k].setdefault(period, []).append(r)
    for k, rs in groups.items():
        rs = np.array(rs)
        lo, hi = ci(rs)
        print(f'  {k:20s} {len(rs):5d} сд {rs.mean():+.3f} [{lo:+.3f}; {hi:+.3f}]  '
              + ' '.join(f'{p} {np.mean(per[k].get(p, [np.nan])):+.3f}' for p in E.PERIODS))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
