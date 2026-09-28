"""
Закономерности для ПАДАЮЩЕГО рынка — записаны до замера (28.09.2026).

Зачем: тетрадь v1 (P1 отскок после разгрузки, P2 накопление при расхождении с
BTC) на test 2025-05…2026-09 (падающий рынок) дала ноль; владельцу нужно 6–8
сделок в неделю в плюс. Пороги — перцентили train (исхода не знают). Режим
«падающий рынок» — BTC за 30 дней ниже −10%.

Гипотезы (механизм):
    B1  продажа отскока: рынок падает, монета отскочила за сутки сильнее 90%
        случаев, ОИ растёт, толпа докупает — ралли в медвежьем рынке продают;
    B2  продолжение пробоя вниз: монета у дна 7 дней, ОИ растёт (новые шорты),
        агрессор продаёт;
    B3  зеркало P2: монета слабее BTC на 2.5%+, пока BTC растёт на 1%+, ОИ и
        объём растут — кто-то раздаёт монету на отскоке рынка;
    B4  толпа покупает падение: доля счетов в лонге у верха за 30 дней, монета
        за сутки вниз;
    B5  продолжение разгрузки: широкая разгрузка (как P1), но на падающем
        рынке — шорт на продолжение, а не отскок;
    G1  P1 с фильтром режима: отскок только когда BTC за 30 дней не ниже −10%.
Приёмка правил с фильтром режима: train R > 0, t ≥ 2, ≥ 0.3 сд/нед; valid
R ≥ 0 при ≥ 20 сделках (меньше — не свидетельство); итог — test, один раз.

    python research/ai_claude_bear.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_pattern_lab as L                            # noqa: E402

BEAR = ['btc_ret_30d', '<', -10.0]


def with_breadth(data):
    cond = [['ret_4h', '<', -4.2604], ['oi_chg_4h', '<', -4.7381]]
    trig = pd.DataFrame({p: pd.Series(L.signal_mask(f, cond), index=f.index) for p, (d, f) in data.items()}).fillna(False)
    breadth = trig.rolling(3, min_periods=1).max().sum(axis=1)
    for p, (d, f) in data.items():
        f['cascade_count_3h'] = breadth.reindex(f.index).to_numpy()
    return data


def rules(q):
    return [
        {'name': 'B1 продажа отскока', 'side': 'short', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [BEAR, ['ret_24h', '>', q('ret_24h', 0.90)], ['oi_chg_24h', '>', q('oi_chg_24h', 0.70)],
                        ['buy_ratio_chg_24h', '>', 0.0]]},
        {'name': 'B2 продолжение пробоя вниз', 'side': 'short', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [BEAR, ['range_pos_7d', '<', 0.05], ['oi_chg_24h', '>', q('oi_chg_24h', 0.70)],
                        ['taker_24h', '<', q('taker_24h', 0.40)]]},
        {'name': 'B3 зеркало P2: раздача на отскоке', 'side': 'short', 'hold_hours': 48, 'stop_atr': 1.3,
         'conditions': [['btc_ret_30d', '<', 0.0], ['rel_24h', '<=', -2.5], ['btc_ret_24h', '>=', 1.0],
                        ['oi_chg_24h', '>=', 4.0], ['vol_z', '>=', 1.5]]},
        {'name': 'B4 толпа покупает падение', 'side': 'short', 'hold_hours': 48, 'stop_atr': 1.0,
         'conditions': [BEAR, ['buy_ratio_pct_30d', '>', 0.90], ['ret_24h', '<', 0.0]]},
        {'name': 'B5 продолжение разгрузки', 'side': 'short', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [BEAR, ['ret_4h', '<', -4.2604], ['oi_chg_4h', '<', -4.7381], ['cascade_count_3h', '>=', 4]]},
        {'name': 'G1 P1 не на падающем рынке', 'side': 'long', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [['btc_ret_30d', '>=', -10.0], ['ret_4h', '<', -4.2604], ['oi_chg_4h', '<', -4.7381],
                        ['cascade_count_3h', '>=', 4]]},
    ]


def accepted(s):
    tr, va = s['train'], s['valid']
    ok_train = tr['n'] > 0 and tr['r'] > 0 and (tr['t'] or 0) >= 2.0 and tr['per_week'] >= 0.3
    ok_valid = va['n'] < 20 or (va['r'] is not None and va['r'] >= 0)
    return ok_train and ok_valid


def main():
    data = with_breadth(L.load_all())
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS['train'])
    allf = pd.concat([f[(f.index >= a) & (f.index < b)] for _, (d, f) in data.items()])
    q = lambda name, x: float(allf[name].quantile(x))    # noqa: E731
    for r in rules(q):
        s, tr = L.evaluate(r, data=data, splits=('train', 'valid'))
        tr = tr[tr['t'] < pd.Timestamp('2025-05-01', tz='UTC')].copy()
        tr['half'] = tr['t'].dt.year.astype(str) + 'H' + ((tr['t'].dt.month > 6) + 1).astype(str)
        halves = ' | '.join(f'{h}: {int(n)} {m:+.2f}' for h, (n, m) in tr.groupby('half')['r'].agg(['size', 'mean']).iterrows())
        print(L.line(r['name'], s), 'ПРИНЯТО' if accepted(s) else '', flush=True)
        print(f'      {halves}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
