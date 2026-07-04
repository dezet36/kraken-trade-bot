"""
geometry_grid.py — сетка риск-геометрии v2 (пересмотр методички, разрешение
пользователя 2026-07-04; чекпоинт v1 = git tag strategy-v1-fibo).

Grid A (фикс-TP): SL-уровень {0.618 (v1) / 0.786 / 0.886 (=инвалидация) / 1.0}
                  × TP {0.18 (v1) / 0.25 / 0.35 / 0.50}
Grid B (трейлинг): SL {0.618 / 0.886} × K {1.5 / 2.0 / 3.0}
                  (после пробоя B фикс-TP отменяется, стоп трейлится на
                   K × исходную SL-дистанцию от экстремума)

Все конфиги: D1B + сессионный фильтр 12-16 UTC (подтверждён oos), 12 мес,
10 валидированных пар, cap=3, risk 0.5%. MIN_RR-гейт ОТКЛЮЧЁН (0) — геометрия
сама определяет RR; эффект отключения гейта изолирует контроль A_sl618_tp18_norr.

Запуск партии:  python geometry_grid.py <cfg1> <cfg2> ...
Сводный отчёт:  python geometry_grid.py --report
"""
import json
import os
import sys
import time

import pandas as pd

import backtest_campaign as camp
import bt12

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}
RISK, CAP = 0.5, 3
SESS = frozenset({12, 13, 14, 15, 16})

BASE_KNOBS = {'BLOCK_BIRTH_HOURS': SESS, 'BLOCK_FILL_HOURS': frozenset(),
              'ENTRY_R_E1A': None, 'BE_EXT': 0.0, 'HTF_STRICT': False,
              'DIR_CAP': None, 'MIN_IMPULSE_PCT': 3.0, 'COOLDOWN_H': 12,
              'MIN_RR_E1': 0.0,
              'SL_R_E1A': None, 'TRAIL_AFTER_BE_K': None, 'TP1_EXT': 0.18}

CONFIGS = {}
# контроль: v1-геометрия без RR-гейта (изолирует эффект отключения гейта)
CONFIGS['A_sl618_tp18_norr'] = {}
for sl_name, sl_v in [('618', None), ('786', 0.786), ('886', 0.886), ('100', 1.0)]:
    for tp in (0.18, 0.25, 0.35, 0.50):
        if sl_name == '618' and tp == 0.18:
            continue   # уже есть как контроль _norr
        CONFIGS[f'A_sl{sl_name}_tp{int(tp*100)}'] = {'SL_R_E1A': sl_v, 'TP1_EXT': tp}
for sl_name, sl_v in [('618', None), ('886', 0.886)]:
    for k in (1.5, 2.0, 3.0):
        CONFIGS[f'B_sl{sl_name}_k{int(k*10)}'] = {'SL_R_E1A': sl_v, 'TRAIL_AFTER_BE_K': k}


def flat(trades):
    if not trades:
        return dict(n=0, sumR=0.0, pf_r=None, wr=0.0)
    rs = [t['R'] for t in trades]
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    return dict(n=len(rs), sumR=round(sum(rs), 1),
                pf_r=round(gp / gl, 3) if gl > 0 else None,
                wr=round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1))


def run_one(name, data_1h, data_5m, T0, MID):
    for k, v in BASE_KNOBS.items():
        setattr(camp, k, v)
    for k, v in CONFIGS[name].items():
        setattr(camp, k, v)

    trd = []
    for p in bt12.PAIRS10:
        trd.extend(bt12.sim_trades(p, data_1h[p], data_5m[p],
                                   data_1h.get(p + '_4h'), CFG, f'v2_{name}'))
    trd = [t for t in trd if pd.Timestamp(t['entry_time']) >= T0]

    capped, _, _ = camp.portfolio_filter(trd, cap=CAP)
    capped, curve, ruined = camp.build_equity(capped, risk_pct=RISK)
    fm = flat(capped)
    pnl = (curve[-1] - curve[0]) / curve[0] * 100 if capped else 0.0
    peak = curve[0]; mdd = 0.0
    for b in curve:
        peak = max(peak, b)
        mdd = max(mdd, (peak - b) / peak * 100)
    h1 = flat([t for t in capped if pd.Timestamp(t['entry_time']) < MID])
    h2 = flat([t for t in capped if pd.Timestamp(t['entry_time']) >= MID])
    avg_rr = round(sum(t['rr'] for t in capped) / len(capped), 2) if capped else 0

    out = {'config': name, 'knobs': {k: v for k, v in CONFIGS[name].items()},
           **fm, 'pnl_pct': round(pnl, 1), 'max_dd_pct': round(mdd, 1),
           'avg_rr': avg_rr, 'ruined': ruined,
           'h1_sumR': h1['sumR'], 'h1_pf': h1['pf_r'], 'h1_n': h1['n'],
           'h2_sumR': h2['sumR'], 'h2_pf': h2['pf_r'], 'h2_n': h2['n']}
    with open(rf'D:\Bot trade\exp_v2_{name}.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"{name:<20} n={fm['n']:<5} WR={fm['wr']:4.1f}% PF={fm['pf_r'] or 0:5.3f} "
          f"PnL={pnl:+7.1f}% DD={mdd:4.1f}% rr~{avg_rr:4.1f} "
          f"| H1 {h1['sumR']:+7.1f} | H2 {h2['sumR']:+7.1f}{' RUIN' if ruined else ''}")
    return out


def report():
    rows = []
    for name in CONFIGS:
        f = rf'D:\Bot trade\exp_v2_{name}.json'
        if os.path.exists(f):
            rows.append(json.load(open(f, encoding='utf-8')))
    if not rows:
        print('Нет результатов.')
        return
    df = pd.DataFrame(rows).sort_values('pnl_pct', ascending=False)
    cols = ['config', 'n', 'wr', 'pf_r', 'pnl_pct', 'max_dd_pct', 'avg_rr',
            'h1_sumR', 'h2_sumR', 'ruined']
    print(df[cols].to_string(index=False))
    df.to_csv(r'D:\Bot trade\backtest_v2_grid.csv', index=False)
    print(f'\nСохранено: backtest_v2_grid.csv ({len(df)}/{len(CONFIGS)} конфигов) | '
          f'Ориентир v1 (D1B+сессия, RR-гейт 2.0): +100.7% / DD 23.6% / WR 29.5% / 26.6 сд-нед')


def main():
    if '--report' in sys.argv:
        report()
        return
    names = [a for a in sys.argv[1:] if a in CONFIGS]
    unknown = [a for a in sys.argv[1:] if a not in CONFIGS and a != '--report']
    if unknown:
        print('Неизвестные конфиги:', unknown)
        print('Доступные:', list(CONFIGS))
        sys.exit(1)
    if not names:
        names = list(CONFIGS)
    t0 = time.time()
    data_1h, data_5m = bt12.load(bt12.PAIRS10)
    manifest_end = max(data_5m[p]['timestamp'].iloc[-1] for p in bt12.PAIRS10).tz_localize(None)
    T0 = manifest_end - pd.Timedelta(days=bt12.EVAL_DAYS)
    MID = manifest_end - pd.Timedelta(days=bt12.EVAL_DAYS // 2)
    for name in names:
        run_one(name, data_1h, data_5m, T0, MID)
    print(f'Партия из {len(names)} конфигов: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    main()
