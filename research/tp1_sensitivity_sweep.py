"""
tp1_sensitivity_sweep.py — сенситивность TP1 на точном live-конфиге (D1B).

Мотивация: WR~29% и RR~2.28 почти зафиксированы геометрией зон (SL за 61.8%,
TP за B), а PF (~1.09) тонкий. Классический рычаг для такой геометрической
стратегии — дистанция TP1 (сейчас TP1_LEVEL=0.18 и в Live_Bot/config.py, и как
TP1_EXT в backtest_campaign.py): ближе TP -> выше WR, но тоньше каждый выигрыш
(ниже RR); дальше TP -> обратный эффект. Ищем, есть ли точка лучше текущей -18%.

Держит фиксированными: e1b=False, e2=False, bos=False, ntp=1, be=True (= D1B,
точный live-конфиг), risk=0.5%/cap=3 (точка деплоя). Варьирует ТОЛЬКО TP1_EXT.

MIN_RR_E1=2.0 остаётся гейтом как в проде — при слишком близком TP1 многие
сетапы просто не пройдут RR-фильтр и выборка поредеет (это тоже часть ответа).

НЕ трогает Live_Bot/. Данные — из backtest_cache/, сети не будет.
"""
import math
import time

import pandas as pd

import backtest as bt
import backtest_campaign as camp

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}   # D1B (live)
DEPLOY_RISK, DEPLOY_CAP = 0.5, 3
CANDIDATES = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25, 0.27, 0.30]

ORIG_TP1_EXT = camp.TP1_EXT   # восстановим после прогона


def _metrics(capped, curve):
    if not capped:
        return dict(n=0, wr=0.0, pf=None, avg_r=0.0, pnl_pct=0.0, mdd=0.0)
    df = pd.DataFrame(capped)
    wr = (df['R'] > 0).mean() * 100
    gp = df[df['pnl'] > 0]['pnl'].sum()
    gl = abs(df[df['pnl'] < 0]['pnl'].sum())
    pf = gp / gl if gl > 0 else math.inf
    avg_r = df['R'].mean()
    pnl_pct = (curve[-1] - curve[0]) / curve[0] * 100
    peak = curve[0]; mdd = 0.0
    for b in curve:
        peak = max(peak, b)
        mdd = max(mdd, (peak - b) / peak * 100)
    return dict(n=len(df), wr=wr, pf=pf, avg_r=avg_r, pnl_pct=pnl_pct, mdd=mdd)


def run():
    t0 = time.time()
    data_1h, data_5m = bt.load_all_data()

    rows = []
    print(f'\n{"TP1_EXT":>8} {"сделок":>7} {"WR%":>6} {"PF":>6} {"avgR":>7} {"Return%":>9} {"MaxDD%":>8}')
    print('-' * 60)

    try:
        for tp1 in CANDIDATES:
            camp.TP1_EXT = tp1
            trd = []
            for pair in bt.BACKTEST_PAIRS:
                trd.extend(camp.simulate_pair_campaign(
                    data_1h[pair], data_5m[pair], data_1h.get(pair + '_4h'), pair, CFG))

            capped, _, _ = camp.portfolio_filter(trd, cap=DEPLOY_CAP)
            capped, curve, ruined = camp.build_equity(capped, risk_pct=DEPLOY_RISK)
            m = _metrics(capped, curve)
            rows.append({'tp1_ext': tp1, 'n_trades': m['n'], 'wr_pct': round(m['wr'], 1),
                         'pf': round(m['pf'], 3) if m['pf'] and math.isfinite(m['pf']) else None,
                         'avg_R': round(m['avg_r'], 3), 'pnl_pct': round(m['pnl_pct'], 1),
                         'max_dd_pct': round(m['mdd'], 1), 'ruined': ruined})

            pf_str = f"{m['pf']:.3f}" if m['pf'] and math.isfinite(m['pf']) else 'inf'
            mark = '  <- текущий live' if abs(tp1 - 0.18) < 1e-9 else ''
            print(f'{tp1:>8.2f} {m["n"]:>7} {m["wr"]:>5.1f}% {pf_str:>6} '
                  f'{m["avg_r"]:>+6.3f}R {m["pnl_pct"]:>+8.1f}% {m["mdd"]:>7.1f}%{mark}')
    finally:
        camp.TP1_EXT = ORIG_TP1_EXT   # не оставляем модуль пропатченным

    df = pd.DataFrame(rows)
    df.to_csv(r'D:\Bot trade\research\results\tp1_sensitivity_sweep.csv', index=False)
    print(f'\nСохранено: tp1_sensitivity_sweep.csv ({len(df)} строк)')

    best_pf = df.loc[df['pf'].astype(float).idxmax()] if df['pf'].notna().any() else None
    best_pnl = df.loc[df['pnl_pct'].idxmax()]
    print(f'\nЛучший PF:     TP1_EXT={best_pf["tp1_ext"]:.2f} -> PF={best_pf["pf"]}, '
          f'return={best_pf["pnl_pct"]:+.1f}%, DD={best_pf["max_dd_pct"]:.1f}%' if best_pf is not None else '')
    print(f'Лучший Return: TP1_EXT={best_pnl["tp1_ext"]:.2f} -> return={best_pnl["pnl_pct"]:+.1f}%, '
          f'PF={best_pnl["pf"]}, DD={best_pnl["max_dd_pct"]:.1f}%')
    print(f'\nВремя: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    run()
