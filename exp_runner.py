"""
exp_runner.py — раннер одиночных экспериментов над стратегией (конфиг D1B,
risk=0.5%/cap=3 — точка реального деплоя). Каждый эксперимент меняет ОДНУ ручку
относительно базы, чтобы вклад фактора был читаем.

Использование:  python exp_runner.py <имя_эксперимента>
Результат:      exp_<имя>.json (метрики + per-pair + половины периода) + консоль.

Ручки монки-патчатся в модуле backtest_campaign (см. блок "Экспериментальные
ручки" там). Живой бот не затрагивается.
"""
import json
import math
import sys
import time

import pandas as pd

import backtest as bt
import backtest_campaign as camp

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}   # D1B (live)
RISK, CAP = 0.5, 3

EXPERIMENTS = {
    # регрессия: дефолтные ручки => должно дать ровно базовый D1B (670 сделок, +23.5%)
    'baseline':        {},
    # вход глубже (Phase A: 67% сделок стопятся не дойдя до B; быстрые стоп-ауты -141R)
    'entry_044':       {'ENTRY_R_E1A': 0.44},
    'entry_050':       {'ENTRY_R_E1A': 0.50},
    'entry_056':       {'ENTRY_R_E1A': 0.56},
    # сессионный фильтр (Phase A: часы 12-16 UTC убыточны в 5/6 месяцев, PF 0.63/0.89 по половинам)
    'sess_fill_12_16': {'BLOCK_FILL_HOURS':  frozenset({12, 13, 14, 15, 16})},
    'sess_birth_12_16': {'BLOCK_BIRTH_HOURS': frozenset({12, 13, 14, 15, 16})},
    # безубыток позже (на полпути к TP -9% вместо уровня B)
    'be_ext_009':      {'BE_EXT': 0.09},
    # только по тренду (блок NEUTRAL)
    'htf_strict':      {'HTF_STRICT': True},
    # чаще торговать (компенсатор частоты для фильтров)
    'cooldown8':       {'COOLDOWN_H': 8},
    # только сильные импульсы
    'imp4':            {'MIN_IMPULSE_PCT': 4.0},
    # лучший TP из sweep — для сравнения в общей таблице
    'tp025':           {'TP1_EXT': 0.25},
    # комбо из факторов, прошедших отбор (гладкий TP-эффект + устойчивый сессионный)
    'combo_tp025_sess': {'TP1_EXT': 0.25,
                         'BLOCK_BIRTH_HOURS': frozenset({12, 13, 14, 15, 16})},
}


def metrics(capped, curve):
    if not capped:
        return dict(n_trades=0, wr_pct=0.0, pf=None, avg_R=0.0, pnl_pct=0.0, max_dd_pct=0.0)
    df = pd.DataFrame(capped)
    wr = (df['R'] > 0).mean() * 100
    gp = df[df['pnl'] > 0]['pnl'].sum()
    gl = abs(df[df['pnl'] < 0]['pnl'].sum())
    pf = gp / gl if gl > 0 else math.inf
    pnl_pct = (curve[-1] - curve[0]) / curve[0] * 100
    peak = curve[0]; mdd = 0.0
    for b in curve:
        peak = max(peak, b)
        mdd = max(mdd, (peak - b) / peak * 100)
    return dict(n_trades=len(df), wr_pct=round(wr, 1),
                pf=round(pf, 3) if math.isfinite(pf) else None,
                avg_R=round(df['R'].mean(), 3), pnl_pct=round(pnl_pct, 1),
                max_dd_pct=round(mdd, 1))


def robustness(capped):
    """Пер-парная согласованность + стабильность по половинам периода (flat R)."""
    df = pd.DataFrame(capped)
    df['exit_time'] = pd.to_datetime(df['exit_time'])
    by_pair = df.groupby('pair')['R'].sum()
    mid = df['exit_time'].min() + (df['exit_time'].max() - df['exit_time'].min()) / 2
    h1, h2 = df[df['exit_time'] < mid], df[df['exit_time'] >= mid]
    return {
        'pairs_positive': int((by_pair > 0).sum()),
        'pairs_total': int(len(by_pair)),
        'top_pair_share_pct': round(
            df.loc[df['R'] > 0].groupby('pair')['R'].sum().max()
            / max(df.loc[df['R'] > 0, 'R'].sum(), 1e-9) * 100, 1),
        'h1_sumR': round(h1['R'].sum(), 1), 'h1_n': len(h1),
        'h2_sumR': round(h2['R'].sum(), 1), 'h2_n': len(h2),
    }


def main():
    name = sys.argv[1]
    knobs = EXPERIMENTS[name]
    for k, v in knobs.items():
        setattr(camp, k, v)

    t0 = time.time()
    data_1h, data_5m = bt.load_all_data()

    trd = []
    for pair in bt.BACKTEST_PAIRS:
        trd.extend(camp.simulate_pair_campaign(
            data_1h[pair], data_5m[pair], data_1h.get(pair + '_4h'), pair, CFG))

    capped, _, _ = camp.portfolio_filter(trd, cap=CAP)
    capped, curve, ruined = camp.build_equity(capped, risk_pct=RISK)

    m = metrics(capped, curve)
    r = robustness(capped) if capped else {}
    out = {'experiment': name, 'knobs': {k: sorted(v) if isinstance(v, frozenset) else v
                                          for k, v in knobs.items()},
           **m, 'ruined': ruined, **r,
           'raw_trades': len(trd), 'minutes': round((time.time() - t0) / 60, 1)}

    with open(rf'D:\Bot trade\exp_{name}.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f'\n=== {name} ===')
    for k, v in out.items():
        print(f'  {k}: {v}')


if __name__ == '__main__':
    main()
