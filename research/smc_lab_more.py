"""
SMC: больше сделок без потери плюса (27.09.2026). Владелец: «а увеличить
количество сделок?» — SMC с фильтром толпы (порог −1 б.п.) делает ~45 сделок
в год на 10 парах.

ВАРИАНТЫ ЗАПИСАНЫ ДО ЗАМЕРА (всё — поверх SMC как она торгует):
    M1  20 пар вместо 10 (фильтр толпы мог сделать чужие пары рабочими);
    M2  R:R ядра от 3 вместо 4;
    M3  конфлюенс от 4.0 вместо 4.5;
    M4  кэп в одну сторону 5 и до 8 позиций (кэп 3 / 5 позиций реже нужны при
        вдвое меньшем числе сделок);
    M5  срок заявки 72 ч вместо 48;
    M6  + зоны на 4 ч (слом и направление — день) одним счётом с часовыми;
    M7  + зоны на 15 мин (слом 4 ч, направление день) одним счётом с часовыми;
    M8  20 пар и R:R от 3 вместе.
Срок заявки у 4 ч и 15 мин — 48 свечей своего ТФ, как в research/ai_mtf_smc.py.

Приёмка — как во втором круге: итог в R лучше базы в bear и mid1 (отбор),
затем лучше на mid2 и в 12m или fresh (проверка), и так же при наливе
«насквозь». Дополнительно видно, насколько выросло число сделок.

Запуск (после research/smc_lab.py gen, gen 4h, gen 15m --pool10):
    python research/smc_lab_more.py            # все варианты, для которых есть данные
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_lab_eval as E                              # noqa: E402
from common import ci                                 # noqa: E402

CROWD = {'filt': lambda r, f: not (f.get('funding', np.nan) > -1.0)}

VARIANTS = [
    ('M1 20 пар', {'pairs': E.ALL20}),
    ('M2 R:R от 3', {'min_rr': 3.0}),
    ('M3 конфлюенс от 4.0', {'min_conf': 4.0}),
    ('M4 кэп 5, до 8 позиций', {'cap': 5, 'max_positions': 8}),
    ('M5 срок заявки 72 ч', {'expiry': 72.0}),
    ('M6 + зоны 4 ч', {'layouts': ('1h', '4h')}),
    ('M7 + зоны 15 мин', {'layouts': ('1h', '15m')}),
    ('M8 20 пар и R:R от 3', {'pairs': E.ALL20, 'min_rr': 3.0}),
]


def have(change):
    for layout in change.get('layouts', ('1h',)):
        for period in E.PERIODS:
            suffix = '' if layout == '1h' else f'_{layout}'
            if not os.path.exists(os.path.join(E.OUT, f'smc_lab_rows_{period}{suffix}.pkl')):
                return False
    return True


def run(change, label):
    spec = dict(E.LIVE, **CROWD, **change)
    per, allr, by_layout = {}, [], {}
    for period in E.PERIODS:
        res, orders = E.portfolio(period, spec)
        layout_of = {o.key: o.meta.get('layout', '1h') for o in orders}
        rs = []
        for t in res['trades']:
            r = t['pnl'] / t['risk']
            rs.append(r)
            by_layout.setdefault(layout_of.get(t['key'], '1h'), []).append(r)
        per[period] = float(np.sum(rs)) if rs else 0.0
        allr += rs
    r = np.array(allr)
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    split = ''
    if len(by_layout) > 1:
        split = '  [' + ', '.join(f'{k}: {len(v)} сд {np.sum(v):+.1f}R' for k, v in sorted(by_layout.items())) + ']'
    print(f'  {label:26s} {len(r):4d} сд {r.sum():+7.1f}R {r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}]  '
          + ' '.join(f'{p} {per[p]:+6.1f}' for p in E.PERIODS) + split, flush=True)
    return per, len(r)


def main():
    print('SMC КАК ТОРГУЕТ (10 пар, 1 ч, против толпы −1 б.п.) и варианты с большим числом сделок')
    base, n_base = run({}, 'база')
    base_ft, _ = run({'fill_through': 0.0005}, 'база, налив насквозь')
    only = set(sys.argv[1:])
    for label, change in VARIANTS:
        if only and label.split()[0] not in only:
            continue
        if not have(change):
            print(f'  {label:26s} — нет данных стенда (smc_lab.py gen …)', flush=True)
            continue
        per, n = run(change, label)
        early = all(per[p] > base[p] for p in ('bear', 'mid1'))
        late = per['mid2'] > base['mid2'] and (per['12m'] > base['12m'] or per['fresh'] > base['fresh'])
        more = f'сделок ×{n / max(1, n_base):.2f}'
        if not early:
            print(f'      {more}; не отобрано по 2022–24', flush=True)
            continue
        if not late:
            print(f'      {more}; отобрано, но не держится на 2024–26', flush=True)
            continue
        ft, _ = run(dict(change, fill_through=0.0005), '   … налив насквозь')
        ok = ft['mid2'] > base_ft['mid2'] and (ft['12m'] > base_ft['12m'] or ft['fresh'] > base_ft['fresh'])
        print(f'      {more}; {"ПРИНЯТО" if ok else "с наливом насквозь не держится — отклонено"}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
