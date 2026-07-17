"""
funnel_sweep.py — sweep непроверенных звеньев воронки поиска сетапов
на задеплоенном v3-конфиге (16 пар, cap=5, risk 0.5%, сессия, hold336).

Звенья: STALE (ожидание филла; live отменяет через 4ч — модель ждала 72ч),
LOOKBACK (окно детекции), IMP_FRESH_N (свежесть B), MAX_IMPULSE_PCT (климакс-
импульсы), MIN_IMPULSE_VELOCITY / MAX_IMPULSE_CANDLES (W12-пороги на v3),
HTF_FILTER=False (блок контртренда никогда не проверялся на отключение).

Контроль: v3_hold_336 (кэш 16 пар) = +252.3% / DD 17.9% / WR 34.7% / 33.7 сд-нед.

Запуск партии:  python funnel_sweep.py <cfg...>   |   сводка: --report
"""
import json
import os
import pickle
import sys
import time

import pandas as pd

import backtest as bt
import backtest_campaign as camp
import bt12

CFG = {'e1b': False, 'e2': False, 'bos': False, 'ntp': 1, 'be': True}
RISK, CAP = 0.5, 5
SESS = frozenset({12, 13, 14, 15, 16})
POOL16 = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT', 'DOGEUSDT',
          'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'LTCUSDT', 'ZECUSDT', 'SUIUSDT',
          'ARBUSDT', 'DOTUSDT', 'XLMUSDT', 'SHIB1000USDT']

CAMP_BASE = {'BLOCK_BIRTH_HOURS': SESS, 'BLOCK_FILL_HOURS': frozenset(),
             'BLOCK_BIRTH_DOW': frozenset(), 'ENTRY_R_E1A': None, 'BE_EXT': 0.0,
             'HTF_STRICT': False, 'DIR_CAP': None, 'MIN_IMPULSE_PCT': 3.0,
             'COOLDOWN_H': 12, 'MIN_RR_E1': 0.0, 'SL_R_E1A': 0.886,
             'TRAIL_AFTER_BE_K': None, 'TP1_EXT': 0.25, 'MAX_HOLD_H': 336,
             'STALE_HOURS': 72, 'LOOKBACK_1H': 48,
             'MAX_IMPULSE_PCT': None, 'HTF_FILTER': True}
BT_BASE = {'IMP_ANCHOR_SNAP': 0, 'IMP_SOFT_FRACTALS': False,
           'IMP_FRESH_FALLBACK': False, 'IMP_MAX_LEG_RETRACE': None,
           'IMP_FRESH_N': 24, 'MAX_IMPULSE_CANDLES': 24, 'MIN_IMPULSE_VELOCITY': 0.30}

CONFIGS = {
    # ожидание филла (live = 4ч!)
    'stale4':      {'camp': {'STALE_HOURS': 4}},
    'stale12':     {'camp': {'STALE_HOURS': 12}},
    'stale24':     {'camp': {'STALE_HOURS': 24}},
    # окно детекции
    'look36':      {'camp': {'LOOKBACK_1H': 36}},
    'look72':      {'camp': {'LOOKBACK_1H': 72}},
    'look96':      {'camp': {'LOOKBACK_1H': 96}},
    # свежесть точки B
    'fresh12':     {'bt': {'IMP_FRESH_N': 12}},
    'fresh36':     {'bt': {'IMP_FRESH_N': 36}},
    # климакс-фильтр (макс. размер импульса)
    'maxpct10':    {'camp': {'MAX_IMPULSE_PCT': 10.0}},
    'maxpct15':    {'camp': {'MAX_IMPULSE_PCT': 15.0}},
    # W12-пороги на v3
    'vel020':      {'bt': {'MIN_IMPULSE_VELOCITY': 0.20}},
    'vel045':      {'bt': {'MIN_IMPULSE_VELOCITY': 0.45}},
    'maxcand18':   {'bt': {'MAX_IMPULSE_CANDLES': 18}},
    'maxcand36':   {'bt': {'MAX_IMPULSE_CANDLES': 36}},
    # HTF-блок контртренда: выключить
    'htf_off':     {'camp': {'HTF_FILTER': False}},
}


def flat(trades):
    if not trades:
        return dict(n=0, sumR=0.0, pf_r=None, wr=0.0)
    rs = [t['R'] for t in trades]
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    return dict(n=len(rs), sumR=round(sum(rs), 1),
                pf_r=round(gp / gl, 3) if gl > 0 else None,
                wr=round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1))


def evaluate(trd, T0, MID):
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
    return {**fm, 'pnl_pct': round(pnl, 1), 'max_dd_pct': round(mdd, 1), 'ruined': ruined,
            'h1_sumR': h1['sumR'], 'h2_sumR': h2['sumR']}


def bounds():
    manifest = json.load(open(os.path.join(bt12.CACHE_12M, 'manifest.json'), encoding='utf-8'))['pairs']
    t_end = max(pd.Timestamp(v['5m']['last']) for v in manifest.values() if v.get('5m', {}).get('n'))
    return t_end - pd.Timedelta(days=bt12.EVAL_DAYS), t_end - pd.Timedelta(days=bt12.EVAL_DAYS // 2)


def base_row(T0, MID):
    trd = []
    for p in POOL16:
        with open(os.path.join(bt12.CACHE_12M, 'trades', f'v3_hold_336__{p}.pkl'), 'rb') as f:
            trd.extend(pickle.load(f))
    return {'config': 'v3_base(16/cap5)', **evaluate(trd, T0, MID)}


def fmt(o):
    return (f"{o['config']:<18} n={o['n']:<5} {o['n']/52:4.1f}/нед WR={o['wr']:4.1f}% "
            f"PF={o['pf_r'] or 0:5.3f} PnL={o['pnl_pct']:+7.1f}% DD={o['max_dd_pct']:4.1f}% "
            f"| H1 {o['h1_sumR']:+6.1f} | H2 {o['h2_sumR']:+6.1f}")


def main():
    if '--report' in sys.argv:
        T0, MID = bounds()
        rows = [base_row(T0, MID)]
        for name in CONFIGS:
            f = rf'D:\Bot trade\research\results\exp_fs_{name}.json'
            if os.path.exists(f):
                rows.append(json.load(open(f, encoding='utf-8')))
        for o in rows:
            print(fmt(o))
        pd.DataFrame(rows).to_csv(r'D:\Bot trade\research\results\backtest_funnel_sweep.csv', index=False)
        print('\nСохранено: backtest_funnel_sweep.csv')
        return

    names = [a for a in sys.argv[1:] if a in CONFIGS] or list(CONFIGS)
    t0 = time.time()
    data_1h, data_5m = bt12.load(POOL16)
    T0, MID = bounds()
    print(fmt(base_row(T0, MID)) + '   <- контроль')
    for name in names:
        for k, v in CAMP_BASE.items():
            setattr(camp, k, v)
        for k, v in BT_BASE.items():
            setattr(bt, k, v)
        for k, v in CONFIGS[name].get('camp', {}).items():
            setattr(camp, k, v)
        for k, v in CONFIGS[name].get('bt', {}).items():
            setattr(bt, k, v)
        trd = []
        for p in POOL16:
            trd.extend(bt12.sim_trades(p, data_1h[p], data_5m[p],
                                       data_1h.get(p + '_4h'), CFG, f'fs_{name}'))
        out = {'config': name, **evaluate(trd, T0, MID)}
        with open(rf'D:\Bot trade\research\results\exp_fs_{name}.json', 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(fmt(out))
    print(f'Партия из {len(names)}: {(time.time()-t0)/60:.1f} мин')


if __name__ == '__main__':
    main()
