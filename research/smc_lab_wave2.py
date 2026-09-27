"""
SMC, второй круг (27.09.2026): что добавить к «против толпы», чтобы плюс был
больше. База — SMC как торгует с 27.09: 10 пар, стоп от 0.8%, предел
издержек 10%, смещение 0.1%, фандинг не в пользу толпы (≤ 0 в сторону сделки).

ПРАВИЛА ЗАПИСАНЫ ДО ЗАМЕРА, у каждого — причина:
    W1  сила толпы: ставка в сторону сделки ≤ −0.5 / ≤ −1 б.п. — чем сильнее
        толпа против, тем больше топлива (на истории доза монотонна);
    W2  премия фьючерса к индексу против толпы — фандинг платится раз в 8 ч и
        запаздывает, премия — почти в реальном времени: за последний час и
        средняя за сутки ≤ 0 в сторону сделки;
    W3  толпа набирает позиции против нас: открытый интерес за сутки растёт, а
        цена за сутки шла против сделки (коррекция к блоку на новых позициях);
    W4  мягче пороги качества под фильтром толпы: конфлюенс от 4.0, R:R от 3;
    W5  кэп в одну сторону 5 вместо 3 — сделок вдвое меньше, кэп жмёт реже;
    W6  размер по силе толпы: ×0.5 при ставке (0; −0.5] б.п., ×1 при (−0.5;
        −1], ×1.5 ниже −1 — ставить больше там, где эффект сильнее.

ПРИЁМКА ВПЕРЁД ПО ВРЕМЕНИ. Правило ОТБИРАЕТСЯ, только если итог лучше базы в
обоих ранних периодах — bear (2022–23) и mid1 (2023–24). Отобранное
ПРИНИМАЕТСЯ, только если оно лучше базы и на периодах, по которым его не
отбирали: mid2 (2024–25) и хотя бы в одном из 12m/fresh, — в том числе при
наливе «насквозь». Для W6 итог — сумма R, взвешенная размером.

Запуск (после research/smc_lab.py gen):
    python research/smc_lab_wave2.py
"""
import os
import sys
from functools import lru_cache

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_doctrine as D                               # noqa: E402
import smc_lab_eval as E                              # noqa: E402
from ai_premium_study import load_premium             # noqa: E402
from common import ci                                 # noqa: E402

H = 3_600_000
EARLY = ('bear', 'mid1')
LATE = ('mid2', '12m', 'fresh')


@lru_cache(maxsize=None)
def _prem(period, pair):
    return load_premium(D.PERIODS[period], pair)


def premium(period, pair, t, side):
    """(последний закрытый час, среднее за сутки) в б.п., знак — в сторону сделки."""
    got = _prem(period, pair)
    if got is None:
        return np.nan, np.nan
    ts, val = got
    k = int(np.searchsorted(ts, t - H, side='right')) - 1
    if k < 24:
        return np.nan, np.nan
    return val[k] * 1e4 * side, float(np.mean(val[k - 23:k + 1])) * 1e4 * side


def crowd(f):
    return f.get('funding', np.nan)


def against(f, limit=0.0):
    x = crowd(f)
    return not (x > limit)                     # нет ставки — не отказ, как в боте


BASE = {'filt': lambda r, f: against(f)}

CANDIDATES = [
    ('W1 ставка ≤ −0.5 б.п.', {'filt': lambda r, f: against(f, -0.5)}),
    ('W1 ставка ≤ −1 б.п.', {'filt': lambda r, f: against(f, -1.0)}),
    ('W2 премия за час ≤ 0', {'filt': lambda r, f: against(f)
                              and not (premium(r.period, r.pair, int(r.t), r.dir)[0] > 0)}),
    ('W2 премия за сутки ≤ 0', {'filt': lambda r, f: against(f)
                                and not (premium(r.period, r.pair, int(r.t), r.dir)[1] > 0)}),
    ('W3 ОИ растёт, цена шла против', {'filt': lambda r, f: against(f)
                                       and f.get('oi_chg_24h', np.nan) > 0 and f.get('ret_24h', np.nan) < 0}),
    ('W4 конфлюенс от 4.0', {'filt': BASE['filt'], 'min_conf': 4.0}),
    ('W4 R:R от 3', {'filt': BASE['filt'], 'min_rr': 3.0}),
    ('W5 кэп 5', {'filt': BASE['filt'], 'cap': 5}),
]


def scale_w6(order):
    f = order.meta.get('feats') or {}
    x = crowd(f)
    if not np.isfinite(x):
        return 1.0
    if x <= -1.0:
        return 1.5
    if x <= -0.5:
        return 1.0
    return 0.5


def run(change, label, periods=E.PERIODS, weighted=False):
    spec = dict(E.LIVE, **change)
    per, allr = {}, []
    for period in periods:
        orders = E.orders_for(period, spec)
        data = E.exec_data(period, spec['pairs'])
        E.smc_engine.FILL_THROUGH_PCT = spec['fill_through']
        try:
            res = E.run_portfolio(orders, data, risk_pct=1.0, max_positions=spec['max_positions'],
                                  cooldown_hours=spec['cooldown'], breakeven_after_tp1=spec['breakeven'],
                                  max_hold_hours=spec['max_hold'], max_same_direction=spec['cap'],
                                  occupy_while_pending=spec['occupy'], cooldown_from_placement=spec['cool_from_place'],
                                  cancel_at_target=spec['cancel_at_target'],
                                  risk_scale=scale_w6 if weighted else None)
        finally:
            E.smc_engine.FILL_THROUGH_PCT = 0.0
        rs = [t['pnl'] / t['risk'] * (t.get('risk_scale', 1.0) if weighted else 1.0) for t in res['trades']]
        per[period] = float(np.sum(rs)) if rs else 0.0
        allr += rs
    r = np.array(allr)
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    print(f'  {label:40s} {len(r):4d} сд {r.sum():+7.1f}R {r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}]  '
          + ' '.join(f'{p} {per[p]:+6.1f}' for p in periods), flush=True)
    return per


def main():
    print('БАЗА — SMC как торгует с 27.09 (10 пар, против толпы)')
    base = run(BASE, 'база')
    base_ft = run(dict(BASE, fill_through=0.0005), 'база, налив насквозь')
    print('\nКАНДИДАТЫ (отбор по bear и mid1; приёмка — mid2 и 12m/fresh, и с наливом насквозь)')
    accepted = []
    items = [(label, change, False) for label, change in CANDIDATES] + \
            [('W6 размер по силе толпы (сумма R × размер)', BASE, True)]
    for label, change, weighted in items:
        per = run(change, label, weighted=weighted)
        early = all(per[p] > base[p] for p in EARLY)
        late = per['mid2'] > base['mid2'] and (per['12m'] > base['12m'] or per['fresh'] > base['fresh'])
        verdict = 'не отобрано по 2022–24' if not early else ('ПРИНЯТО?' if late else 'отобрано, но не держится на 2024–26')
        print(f'      {verdict}', flush=True)
        if early and late:
            ft = run(dict(change, fill_through=0.0005), '   … налив насквозь', weighted=weighted)
            ok = ft['mid2'] > base_ft['mid2'] and (ft['12m'] > base_ft['12m'] or ft['fresh'] > base_ft['fresh'])
            print(f'      с наливом насквозь {"держится — ПРИНЯТО" if ok else "не держится — отклонено"}', flush=True)
            if ok:
                accepted.append(label)
    print('\nПРИНЯТО:', ', '.join(accepted) or 'ничего')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
