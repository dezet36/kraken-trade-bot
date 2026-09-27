"""
Поиск прибыльной SMC на стенде (research/smc_lab.py, smc_lab_eval.py).

СПИСОК ГИПОТЕЗ ЗАПИСАН ДО ЗАМЕРА (27.09.2026). Каждая — с причиной, почему
она могла бы работать. Новые гипотезы после просмотра итогов в этот прогон не
добавляются; если появятся — отдельным прогоном с пометкой «после просмотра».

База — ЖИВАЯ SMC: 10 пар пула, стоп от 0.8% (настройка оператора), предел
издержек 10% риска, смещение лимита 0.1% (config), налив касанием, живые
правила брокера (заявка занимает пару, кэп 3 в сторону, пауза 12 ч с
постановки, до 5 позиций, срок заявки 48 ч, без безубытка, без снятия у цели).

H1  тесные стопы: стоп от 0.5% и предел издержек 15% — плюс старых бэктестов
    был там (п. 64); вопрос — держится ли он с живым смещением лимита.
H2  порог R:R 3 / 5 / 6 вместо 4 — плюс SMC держится на дальних целях.
H3  вход в середину блока (0.5) — лучше цена и R, но реже налив.
H4  без смещения лимита (0) — цена смещения 0.1% в R у тесных стопов велика.
H5  выход: доли 50/25/25 и 34/33/33 вместо 25/25/50; удержание 7 дней.
H6  фильтры рынка на момент заявки:
    a  толпа против сделки — фандинг ≤ 0 (толпа стоит в другую сторону и платит);
    b  BTC за неделю идёт в сторону сделки;
    c  пара за месяц идёт в сторону сделки (с месячным трендом);
    d  спокойный рынок — ранг ATR ниже медианы;
    e  без входов ±24 ч от решения ФРС;
    f  только сессии Лондона и Нью-Йорка (killzone как запрет).
H7  набор пар: все 20 вместо 10.
H8  портфель: кэп 2 и 5 вместо 3; до 8 позиций; пауза 0 и 24 ч.
H9  (отдельным прогоном, research/smc_lab_confirm.py) вход с подтверждением:
    цена дошла до блока, затем 15-минутная свеча закрылась обратно за его
    ближним краем — вход по рынку на закрытии этой свечи, стоп прежний, за
    дальним краем. Классический вход smart money: отсекает блоки, которые цена
    прошивает насквозь, ценой худшей цены входа.

ПРИЁМКА. Правило принимается, только если (1) итог в R лучше базы в каждом из
трёх независимых периодов bear, mid1, mid2 и в недавнем (12m или fresh — они
перекрываются), (2) выигрыш сохраняется при наливе «насквозь на 0.05%», (3)
у правила есть причина. Затем принятые правила объединяются, и объединение
проверяется так же. Отдельно — проверка процедуры отбора вперёд по времени:
лучший вариант, выбранный только по bear+mid1, на mid2 и недавнем.

Запуск:
    python research/smc_lab_study.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smc_lab_eval as E                              # noqa: E402

RECENT = ('12m', 'fresh')
INDEPENDENT = ('bear', 'mid1', 'mid2')

HYPOTHESES = [
    ('H1 стоп от 0.5%, предел 15%', {'min_stop': 0.5, 'cost_limit': 15.0}),
    ('H2 R:R от 3', {'min_rr': 3.0}),
    ('H2 R:R от 5', {'min_rr': 5.0}),
    ('H2 R:R от 6', {'min_rr': 6.0}),
    ('H3 вход в середину блока', {'depth': 0.5}),
    ('H4 без смещения лимита', {'offset': 0.0}),
    ('H5 доли 50/25/25', {'fractions': (0.5, 0.25, 0.25)}),
    ('H5 доли 34/33/33', {'fractions': (0.34, 0.33, 0.33)}),
    ('H5 удержание 7 дней', {'max_hold': 168.0}),
    ('H6a толпа против сделки (фандинг ≤ 0)', {'filt': lambda r, f: f.get('funding', np.nan) <= 0}),
    ('H6b BTC за неделю в сторону сделки', {'filt': lambda r, f: f.get('btc_ret_7d', np.nan) > 0}),
    ('H6c пара за месяц в сторону сделки', {'filt': lambda r, f: f.get('ret_30d', np.nan) > 0}),
    ('H6d спокойный рынок (ранг ATR < 50)', {'filt': lambda r, f: f.get('atr_rank', np.nan) < 50}),
    ('H6e без ФРС ±24 ч', {'filt': lambda r, f: not (f.get('fomc_hours', 1e9) <= 24)}),
    ('H6f только Лондон и Нью-Йорк', {'filt': lambda r, f: bool(getattr(r, 'f_killzone', False))}),
    ('H7 все 20 пар', {'pairs': E.ALL20}),
    ('H8 кэп 2', {'cap': 2}),
    ('H8 кэп 5', {'cap': 5}),
    ('H8 до 8 позиций', {'max_positions': 8}),
    ('H8 пауза 0 ч', {'cooldown': 0.0}),
    ('H8 пауза 24 ч', {'cooldown': 24.0}),
]


def verdict(base, res):
    better = {p: res['per'][p][1] > base['per'][p][1] for p in E.PERIODS}
    ok = all(better[p] for p in INDEPENDENT) and any(better[p] for p in RECENT)
    return ok, sum(better.values())


def main():
    print('БАЗА')
    base = E.run({}, 'живая SMC')
    base_ft = E.run({'fill_through': 0.0005}, 'живая SMC, налив насквозь')
    print('\nГИПОТЕЗЫ (итог в R по периодам; «лучше» — в скольких из 5 периодов лучше базы)')
    accepted = []
    for label, change in HYPOTHESES:
        res = E.run(change, label)
        ok, n_better = verdict(base, res)
        mark = 'ПРИНЯТА?' if ok else ''
        print(f'      лучше базы в {n_better} из 5 {mark}', flush=True)
        if ok:
            ft = E.run(dict(change, fill_through=0.0005), '   … с наливом насквозь')
            ok_ft, n_ft = verdict(base_ft, ft)
            print(f'      с наливом насквозь лучше базы в {n_ft} из 5 {"— ПРИНЯТА" if ok_ft else "— отклонена"}',
                  flush=True)
            if ok_ft:
                accepted.append((label, change))
    print('\nПРИНЯТЫ:', ', '.join(label for label, _ in accepted) or 'ни одна')
    if len(accepted) > 1:
        combo, filters = {}, []
        for _, change in accepted:
            for k, v in change.items():
                if k == 'filt':
                    filters.append(v)
                else:
                    combo[k] = v
        if filters:
            combo['filt'] = lambda r, f: all(g(r, f) for g in filters)
        print('\nОБЪЕДИНЕНИЕ ПРИНЯТЫХ')
        res = E.run(combo, 'объединение')
        E.run(dict(combo, fill_through=0.0005), 'объединение, налив насквозь')
        print(f'      лучше базы в {verdict(base, res)[1]} из 5')
    print('\nПРОЦЕДУРА ОТБОРА ВПЕРЁД ПО ВРЕМЕНИ: лучшая гипотеза по bear+mid1 → mid2, 12m, fresh')
    scores = []
    for label, change in HYPOTHESES:
        res = E.run(change, label, periods=['bear', 'mid1'], quiet=True)
        scores.append((res['sum'], label, change))
    scores.sort(key=lambda x: -x[0])
    best_sum, best_label, best_change = scores[0]
    print(f'   выбрана по bear+mid1: {best_label} ({best_sum:+.1f}R против базы '
          f'{sum(base["per"][p][1] for p in ("bear", "mid1")):+.1f}R)')
    res = E.run(best_change, f'{best_label} — проверка', periods=['mid2', '12m', 'fresh'])
    print('   база на тех же периодах: ' + ' '.join(f'{p} {base["per"][p][1]:+.1f}' for p in ('mid2', '12m', 'fresh')))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
