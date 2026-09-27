"""
Свой поток для ИИ: каркас с плюсом там, где SMC не торгует (27.09.2026).

ЗАЧЕМ. Доктрина ИИ не выходит в плюс ни с одним из девяти ограничений кода
(results/ai_gates_study.txt). Единственный каркас проекта с плюсом на истории —
ядро SMC против толпы (порог −1 б.п.); вариант «после снятия ликвидности»
в плюсе во всех пяти периодах (results/smc_lab_logic.txt). Но на 10 парах SMC
это её же сделки — ИИ удвоил бы ставку на них. Свой поток ИИ возможен только
на других парах.

ЗАПИСАНО ДО ЗАМЕРА. Стенд SMC (research/smc_lab_eval.py, только чтение),
живые правила брокера, исполнение ИИ в режиме «правила»: предел издержек 10%
без смещения лимита, минимальный стоп ядра 0.5% (брокер пропускает от ~0.75%).
    H1  против толпы −1 б.п. + после снятия ликвидности, 10 пар ВНЕ пула SMC
    H2  против толпы −1 б.п., 10 пар вне пула SMC
    R1  (справка) H1 на пуле SMC — насколько сделки ИИ совпали бы с SMC
Приёмка потока ИИ: в плюсе во всех пяти периодах, нижняя граница 95%
интервала > 0, при наливе «насквозь» итог > 0, сделок ≥ 100.

Итог (results/ai_own_stream.txt): вне пула SMC не прошли ни H1, ни H2 (минус
в bear и mid1). Поэтому ИИ торгует пул SMC, и последний шаг — раскладка ровно
как в боте (`final`): окно решений ФРС ±24 ч у режима «правила» уже есть;
оставляем его, если приёмка с ним держится.
    F1  толпа −1 + снятие (= R1)
    F2  толпа −1 + снятие + без входов ±24 ч от решений ФРС (как в боте)
    F3  (справка) толпа −1 + ФРС, без условия снятия

    python research/ai_own_stream.py          # H1, H2, R1
    python research/ai_own_stream.py final    # F2, F3
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_lab_eval as E                              # noqa: E402
from common import ci                                 # noqa: E402


def crowd_ok(f):
    return not (f.get('funding', np.nan) > -1.0)


def swept(r):
    return bool(getattr(r, 'f_liquidity_swept', False))


AI_EXEC = dict(offset=0.0, min_stop=0.5, cost_limit=10.0)


def run(change, label):
    spec = dict(E.LIVE, **AI_EXEC, **change)
    per, allr = {}, []
    for period in E.PERIODS:
        res, _ = E.portfolio(period, spec)
        rs = [t['pnl'] / t['risk'] for t in res['trades']]
        per[period] = (float(np.sum(rs)), len(rs))
        allr += rs
    r = np.array(allr)
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    print(f"  {label:52s} {len(r):4d} сд  {r.sum():+7.1f}R {r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}]  "
          + ' '.join(f'{p} {per[p][0]:+6.1f}({per[p][1]})' for p in E.PERIODS), flush=True)
    return per, r, lo


def far_from_fomc(f):
    return not (f.get('fomc_hours', np.nan) <= 24)


def final():
    for label, filt in (('F2 толпа −1 + снятие + ФРС ±24 ч (как в боте)',
                         lambda r, f: crowd_ok(f) and swept(r) and far_from_fomc(f)),
                        ('F3 (справка) толпа −1 + ФРС ±24 ч, без снятия',
                         lambda r, f: crowd_ok(f) and far_from_fomc(f))):
        per, r, lo = run(dict(filt=filt), label)
        _, r_ft, _ = run(dict(filt=filt, fill_through=0.0005), '   … налив насквозь')
        ok = (all(v[0] > 0 for v in per.values()) and lo > 0 and r_ft.sum() > 0 and len(r) >= 100)
        print(f"      {'ПРИНЯТО' if ok else 'не прошло'}", flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'final':
        final()
        return
    all_pairs = sorted(set().union(*(set(E.rows(p)['pair']) for p in E.PERIODS)))
    outside = tuple(p for p in all_pairs if p not in E.POOL10)
    print('пары вне пула SMC:', ', '.join(outside))
    h1 = dict(pairs=outside, filt=lambda r, f: crowd_ok(f) and swept(r))
    h2 = dict(pairs=outside, filt=lambda r, f: crowd_ok(f))
    for label, change in (('H1 толпа −1 + снятие, вне пула SMC', h1),
                          ('H2 толпа −1, вне пула SMC', h2)):
        per, r, lo = run(change, label)
        _, r_ft, _ = run(dict(change, fill_through=0.0005), '   … налив насквозь')
        ok = (all(v[0] > 0 for v in per.values()) and lo > 0 and r_ft.sum() > 0 and len(r) >= 100)
        print(f"      {'ПРИНЯТО' if ok else 'не прошло'}", flush=True)
    run(dict(filt=lambda r, f: crowd_ok(f) and swept(r)), 'R1 (справка) толпа −1 + снятие, пул SMC')
    run(dict(filt=lambda r, f: crowd_ok(f) and swept(r), fill_through=0.0005), '   … налив насквозь')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
