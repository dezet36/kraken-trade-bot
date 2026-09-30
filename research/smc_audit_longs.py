"""
Аудит SMC (30.09.2026): вернуть ли лонги при слегка отрицательной ставке.

ПОСЛЕ ПРОСМОТРА. Разбивка по ставке (results/smc_audit_funding*.txt)
показала: лонги при ставке от −1 до 0 б.п. — +0.3R на заявку (44–46 заявок),
а порог −1 б.п. их отсекает. Вариант собран по этой разбивке, то есть не
вслепую, — поэтому только через ту же приёмку, что и второй круг
(smc_lab_wave2): отбор по bear и mid1, приёмка на mid2 и 12m/fresh, и то же
при наливе «насквозь». Портфель — «как бот» на окне свечей бота (повтор
каждый час, research/smc_audit_targets.run_retry).

    A  лонг при ставке ≤ 0, шорт при ставке ≥ +1 б.п. (шорты как сейчас)

Запуск (после smc_lab_live.py gen):
    python research/smc_audit_longs.py > research/results/smc_audit_longs.txt
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_lab_eval as E                              # noqa: E402
from smc_audit_funding import passes, signed          # noqa: E402
from smc_audit_targets import fmt, orders_retry       # noqa: E402

EARLY = ('bear', 'mid1')
FT = 0.0005


def now_rule(r, f):
    return passes(signed(f))                           # как в боте: ≤ −1 б.п. в сторону сделки


def variant_a(r, f):
    x = signed(f)
    if not np.isfinite(x):
        return True
    return x <= 0.0 if r.dir > 0 else x <= -1.0       # лонг: сырая ≤ 0; шорт: сырая ≥ +1


def run(filt, label, fill_through=0.0):
    spec = dict(E.LIVE, filt=filt, fill_through=fill_through)
    per, allr = {}, []
    for period in E.PERIODS:
        res, _ = E.portfolio(period, spec, orders=orders_retry(period, spec, 'live'))
        rs = [t['pnl'] / t['risk'] for t in res['trades']]
        per[period] = sum(rs)
        allr += rs
    print(f'  {label:44s} {fmt(allr)}  ' + ' '.join(f'{p} {per[p]:+6.1f}' for p in E.PERIODS), flush=True)
    return per


def main():
    E.BAR_H.setdefault('live', 1.0)
    print('Портфель «как бот» на окне свечей бота (повтор каждый час), 10 пар, живые правила')
    base = run(now_rule, 'как сейчас: ≤ −1 б.п. обе стороны')
    base_ft = run(now_rule, '   … налив насквозь', FT)
    a = run(variant_a, 'A: лонг ≤ 0, шорт ≥ +1 б.п.')
    early = all(a[p] > base[p] for p in EARLY)
    late = a['mid2'] > base['mid2'] and (a['12m'] > base['12m'] or a['fresh'] > base['fresh'])
    print(f'      отбор по bear и mid1: {"да" if early else "нет"}; приёмка на mid2 и 12m/fresh: {"да" if late else "нет"}')
    a_ft = run(variant_a, '   … налив насквозь', FT)
    late_ft = a_ft['mid2'] > base_ft['mid2'] and (a_ft['12m'] > base_ft['12m'] or a_ft['fresh'] > base_ft['fresh'])
    print(f'      насквозь: приёмка {"да" if late_ft else "нет"}')
    print('ПРИНЯТО' if early and late and late_ft else 'НЕ ПРИНЯТО')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
