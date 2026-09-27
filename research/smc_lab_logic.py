"""
SMC по логике владельца (27.09.2026): «главное — сетап с R:R 3 к 1 и логика:
снятие ликвидности, тест имбаланса, ордер-блок». Оценка — в R на сделку, не в
доле депозита.

ВАРИАНТЫ ЗАПИСАНЫ ДО ЗАМЕРА. У каждого сетапа стенда уже записано, была ли
перед ним снята ликвидность (фактор liquidity_swept) и есть ли у блока
имбаланс (fvg_present):
    L1  вход только после снятия ликвидности;
    L2  только блоки с имбалансом;
    L3  снятие ликвидности И имбаланс;
    L4  R:R ядра от 3 вместо 4;
    L5  сетап «3 к 1»: структура даёт не меньше 3R (R:R ядра ≥ 3), выход —
        одна цель ровно на 3R;
    L6  полная модель: снятие + имбаланс + блок + «3 к 1».
Каждый — поверх SMC как она торгует (против толпы, порог −1 б.п.) и, для
понимания вклада самой логики, поверх SMC без фильтра толпы.

Приёмка — как во втором круге: лучше базы в bear и mid1 (отбор), затем лучше
на mid2 и в 12m или fresh (проверка), и так же при наливе «насквозь».

Запуск:
    python research/smc_lab_logic.py
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


def imbalance(r):
    return bool(getattr(r, 'f_fvg_present', False))


LOGIC = [
    ('L1 после снятия ликвидности', lambda r: swept(r), {}),
    ('L2 блок с имбалансом', lambda r: imbalance(r), {}),
    ('L3 снятие + имбаланс', lambda r: swept(r) and imbalance(r), {}),
    ('L4 R:R от 3', lambda r: True, {'min_rr': 3.0}),
    ('L5 «3 к 1»: структура ≥ 3R, цель 3R', lambda r: True, {'min_rr': 3.0, 'fixed_r': 3.0}),
    ('L6 снятие + имбаланс + «3 к 1»', lambda r: swept(r) and imbalance(r), {'min_rr': 3.0, 'fixed_r': 3.0}),
]


def run(change, label, periods=E.PERIODS):
    spec = dict(E.LIVE, **change)
    per, allr = {}, []
    for period in periods:
        res, _ = E.portfolio(period, spec)
        rs = [t['pnl'] / t['risk'] for t in res['trades']]
        per[period] = float(np.sum(rs)) if rs else 0.0
        allr += rs
    r = np.array(allr)
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    wins = r[r > 0]
    losses = r[r <= 0]
    print(f'  {label:44s} {len(r):4d} сд  в плюс {np.mean(r > 0) * 100 if len(r) else 0:4.1f}%  '
          f'выигрыш {wins.mean() if len(wins) else 0:+.2f}R проигрыш {losses.mean() if len(losses) else 0:+.2f}R  '
          f'{r.sum():+7.1f}R {r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}]  '
          + ' '.join(f'{p} {per[p]:+6.1f}' for p in periods), flush=True)
    return per


def verdict(base, per):
    early = all(per[p] > base[p] for p in ('bear', 'mid1'))
    late = per['mid2'] > base['mid2'] and (per['12m'] > base['12m'] or per['fresh'] > base['fresh'])
    return early, late


def main():
    for crowd_label, use_crowd in (('SMC КАК ТОРГУЕТ: против толпы, порог −1 б.п.', True),
                                   ('ДЛЯ ПОНИМАНИЯ: SMC БЕЗ фильтра толпы', False)):
        print(f'\n== {crowd_label}')
        base_filt = (lambda r, f: crowd_ok(f)) if use_crowd else (lambda r, f: True)
        base = run({'filt': base_filt}, 'база')
        base_ft = run({'filt': base_filt, 'fill_through': 0.0005}, 'база, налив насквозь')
        for label, rule, extra in LOGIC:
            if use_crowd:
                filt = (lambda r, f, rule=rule: crowd_ok(f) and rule(r))
            else:
                filt = (lambda r, f, rule=rule: rule(r))
            change = dict(extra, filt=filt)
            per = run(change, label)
            early, late = verdict(base, per)
            if not early:
                print('      не отобрано по 2022–24', flush=True)
                continue
            if not late:
                print('      отобрано, но не держится на 2024–26', flush=True)
                continue
            ft = run(dict(change, fill_through=0.0005), '   … налив насквозь')
            _, late_ft = verdict(base_ft, ft)
            print(f'      {"ПРИНЯТО" if late_ft else "с наливом насквозь не держится — отклонено"}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
