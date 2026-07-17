"""
walk_forward.py — out-of-sample валидация задеплоенного конфига на 12 месяцах.

Схема БЕЗ нарезки данных: одна симуляция на полных 400 днях на конфиг
(D1B и D1B+сессия, старые 10 пар), затем split СДЕЛОК по entry_time:
  T0  = T_END − 360д  (срез 40-дневного прогрева HTF)
  MID = T_END − 180д
  H1  = [T0, MID)   — out-of-sample (никто не видел при тюнинге сессии/W12)
  H2  = [MID, T_END] — in-sample (~96% перекрытие с окном настройки)

Плюс шаг dir_cap: portfolio_filter(cap=3, dir_cap=2) на тех же сделках — секунды.

Запуск: python walk_forward.py   (после python bt12.py; повторно — мгновенно, кэш симуляций)
"""
import json
import math
import time

import pandas as pd

import backtest_campaign as camp
import bt12

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}   # D1B (live)
RISK, CAP = 0.5, 3
SESS = frozenset({12, 13, 14, 15, 16})

RUNS = {
    'D1B':      {},
    'D1B_sess': {'BLOCK_BIRTH_HOURS': SESS},
}
# Гигиена module-state: полный сброс ручек перед каждым конфигом
DEFAULTS = {'BLOCK_BIRTH_HOURS': frozenset(), 'BLOCK_FILL_HOURS': frozenset(),
            'ENTRY_R_E1A': None, 'BE_EXT': 0.0, 'HTF_STRICT': False,
            'DIR_CAP': None, 'TP1_EXT': 0.18, 'MIN_IMPULSE_PCT': 3.0, 'COOLDOWN_H': 12}


def flat_metrics(trades):
    """Метрики без компаундинга (сравнимость половин): sumR, PF_R, WR, n."""
    if not trades:
        return dict(n=0, sumR=0.0, pf_r=None, wr=0.0)
    rs = [t['R'] for t in trades]
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    return dict(n=len(rs), sumR=round(sum(rs), 1),
                pf_r=round(gp / gl, 3) if gl > 0 else None,
                wr=round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1))


def eq_metrics(capped, curve):
    """Полный период: компаунд PnL%, maxDD, PF$, WR, n."""
    fm = flat_metrics(capped)
    if not capped:
        return {**fm, 'pnl_pct': 0.0, 'max_dd_pct': 0.0}
    pnl_pct = (curve[-1] - curve[0]) / curve[0] * 100
    peak = curve[0]; mdd = 0.0
    for b in curve:
        peak = max(peak, b)
        mdd = max(mdd, (peak - b) / peak * 100)
    return {**fm, 'pnl_pct': round(pnl_pct, 1), 'max_dd_pct': round(mdd, 1)}


def run():
    t0 = time.time()
    data_1h, data_5m = bt12.load(bt12.PAIRS10)

    # naive-UTC: внутри сделок времена — numpy datetime64 без tz (ts5.values)
    t_end = max(data_5m[p]['timestamp'].iloc[-1] for p in bt12.PAIRS10).tz_localize(None)
    T0  = t_end - pd.Timedelta(days=bt12.EVAL_DAYS)
    MID = t_end - pd.Timedelta(days=bt12.EVAL_DAYS // 2)
    print(f'T_END={t_end} | T0={T0} | MID={MID}')

    out, trd_by_key = {}, {}
    for key, knobs in RUNS.items():
        for k, v in DEFAULTS.items():
            setattr(camp, k, v)
        for k, v in knobs.items():
            setattr(camp, k, v)

        trd = []
        for p in bt12.PAIRS10:
            trd.extend(bt12.sim_trades(p, data_1h[p], data_5m[p],
                                       data_1h.get(p + '_4h'), CFG, key))
        trd = [t for t in trd if pd.Timestamp(t['entry_time']) >= T0]
        trd_by_key[key] = trd

        if key == 'D1B_sess':   # self-check сессионного фильтра
            bad = [t for t in trd if pd.Timestamp(t['birth_time']).hour in SESS]
            assert not bad, f'сессионный фильтр протёк: {len(bad)} сделок рождены в 12-16 UTC'

        capped, _, _ = camp.portfolio_filter(trd, cap=CAP)
        capped, curve, ruined = camp.build_equity(capped, risk_pct=RISK)
        h1 = [t for t in capped if pd.Timestamp(t['entry_time']) < MID]
        h2 = [t for t in capped if pd.Timestamp(t['entry_time']) >= MID]
        out[key] = {'full': eq_metrics(capped, curve), 'ruined': ruined,
                    'H1_oos': flat_metrics(h1), 'H2_ins': flat_metrics(h2)}
        pd.DataFrame(capped).to_csv(rf'D:\Bot trade\research\results\backtest_wf_{key}.csv', index=False)

        f, a, b = out[key]['full'], out[key]['H1_oos'], out[key]['H2_ins']
        print(f"\n[{key}] full: n={f['n']} WR={f['wr']}% PF_R={f['pf_r']} "
              f"PnL={f['pnl_pct']:+.1f}% DD={f['max_dd_pct']}%")
        print(f"  H1 (out-of-sample): n={a['n']} WR={a['wr']}% sumR={a['sumR']:+.1f} PF_R={a['pf_r']}")
        print(f"  H2 (in-sample):     n={b['n']} WR={b['wr']}% sumR={b['sumR']:+.1f} PF_R={b['pf_r']}")

    # ── Диагностика механизма: часы рождения в H1 у БАЗОВОГО прогона ─────────
    base_h1 = [t for t in trd_by_key['D1B'] if pd.Timestamp(t['entry_time']) < MID]
    in_sess  = [t for t in base_h1 if pd.Timestamp(t['birth_time']).hour in SESS]
    out_sess = [t for t in base_h1 if pd.Timestamp(t['birth_time']).hour not in SESS]
    print(f"\nH1 базового прогона по часам рождения (raw, до portfolio_filter):")
    print(f"  рождено в 12-16 UTC: {flat_metrics(in_sess)}")
    print(f"  рождено вне:         {flat_metrics(out_sess)}")

    # ── Вердикт сессионного фильтра (по H1, знак эффекта) ────────────────────
    b_h1, s_h1 = out['D1B']['H1_oos'], out['D1B_sess']['H1_oos']
    d_sumr = s_h1['sumR'] - b_h1['sumR']
    pf_ok = (s_h1['pf_r'] or 0) >= (b_h1['pf_r'] or 0)
    if d_sumr >= 0 and pf_ok:
        verdict = 'CONFIRMED'
    elif d_sumr < -5 and not pf_ok:
        verdict = 'INVERTED'
    else:
        verdict = 'GRAY_ZONE'
    print(f"\nВЕРДИКТ сессионного фильтра (H1 out-of-sample): {verdict} "
          f"(Δ sumR={d_sumr:+.1f}R, PF {b_h1['pf_r']} -> {s_h1['pf_r']})")

    # ── Шаг dir_cap: на 12м сделках D1B_sess, без ре-симуляций ───────────────
    print('\n── Направленный кэп (D1B_sess, 12 мес, cap=3) ──')
    dc_out = {}
    trd_sess = trd_by_key['D1B_sess']
    for dc in (None, 2):
        capped, _, dropped = camp.portfolio_filter(trd_sess, cap=CAP, dir_cap=dc)
        capped, curve, _ = camp.build_equity(capped, risk_pct=RISK)
        m = eq_metrics(capped, curve)
        dc_out[str(dc)] = m
        print(f"  dir_cap={dc}: n={m['n']} WR={m['wr']}% PnL={m['pnl_pct']:+.1f}% "
              f"DD={m['max_dd_pct']}% (dropped {dropped})")
    base_m, cap_m = dc_out['None'], dc_out['2']
    dd_cut  = (base_m['max_dd_pct'] - cap_m['max_dd_pct']) / max(base_m['max_dd_pct'], 1e-9) * 100
    pnl_cut = (base_m['pnl_pct'] - cap_m['pnl_pct']) / max(abs(base_m['pnl_pct']), 1e-9) * 100
    dc_verdict = 'ACCEPT' if (dd_cut >= 15 and pnl_cut <= 15) else 'REJECT'
    print(f"  ΔDD={dd_cut:+.1f}% (относительное снижение), ΔPnL={pnl_cut:+.1f}% (потеря) "
          f"=> dir_cap=2: {dc_verdict}")

    # регрессия рефакторинга portfolio_filter: dir_cap=None == старое поведение
    a1, _, _ = camp.portfolio_filter(trd_sess, cap=CAP)
    a2, _, _ = camp.portfolio_filter(trd_sess, cap=CAP, dir_cap=None)
    assert [id(t) for t in a1] == [id(t) for t in a2], 'portfolio_filter regression!'

    with open(r'D:\Bot trade\research\results\exp_wf_verdict.json', 'w', encoding='utf-8') as f:
        json.dump({'runs': out, 'session_verdict': verdict,
                   'dircap': dc_out, 'dircap_verdict': dc_verdict,
                   'T0': str(T0), 'MID': str(MID), 'T_END': str(t_end)},
                  f, ensure_ascii=False, indent=2)
    print(f"\nСохранено: exp_wf_verdict.json | Время: {(time.time()-t0)/60:.1f} мин")


if __name__ == '__main__':
    run()
