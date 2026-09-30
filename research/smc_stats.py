"""
SMC без фильтра фандинга: полная статистика на всех парах списка бота
(30.09.2026).

Владелец: «а если убрать ориентир на фандинг — какая успешность сделок?
проанализируй и дай полную статистику. используй все пары, а не только 10».

Сетапы — стенд «глазами бота» (research/smc_lab_live.py, раскладка 'live20':
окно свечей живого бота, его цели и R:R), 20 пар research/ai_doctrine.PAIRS,
какие есть в кэше периода. Правила решений и брокера — как у живой SMC
(smc_lab_eval.LIVE), фильтра толпы нет.

  1. Портфель «как бот» (повтор попытки каждый час): сделки, доля в плюс,
     цели, стопы, R на сделку, фактор прибыли, просадка, серии — по периодам,
     налив касанием и насквозь; 10 пар пула и 10 остальных отдельно.
  2. Каждая заявка отдельно (без портфеля): налив, исход, разрезы по стороне,
     паре, полугодию, часу, дню недели, конфлюенсу, R:R, стопу, каждому
     фактору ядра и признакам рынка.
  3. Ход цены внутри сделки: сколько проигравших успевали уйти в плюс.
Заявки со всеми признаками — results/smc_stats_setups.pkl (для поиска
улучшений, research/smc_improve.py).

Запуск (после smc_lab_live.py gen20 и fetch_fresh20.py):
    python research/smc_stats.py > research/results/smc_stats.txt
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ai_doctrine as D                               # noqa: E402

D.PERIODS['fresh'] = 'backtest_cache_fresh20'         # все 20 пар свежего периода (fetch_fresh20.py)

import smc_lab_eval as E                              # noqa: E402
from common import ci                                 # noqa: E402
from smc_audit_targets import orders_retry            # noqa: E402

LAYOUT = 'live20'
E.BAR_H.setdefault(LAYOUT, 1.0)
FT = 0.0005
ALL = tuple(D.PAIRS)
POOL = tuple(E.POOL10)
OTHER = tuple(p for p in ALL if p not in POOL)
FEATS = ('ret_24h', 'ret_7d', 'ret_30d', 'pos30', 'atr_rank', 'adr_pct', 'vol_ratio', 'dist_entry_pct',
         'stop_adr', 'ema50d_gap', 'er_30d', 'btc_ret_24h', 'btc_ret_7d', 'btc_er_30d', 'hour', 'weekday',
         'funding')


def spec(**changes):
    out = dict(E.LIVE, pairs=ALL, layouts=(LAYOUT,), filt=None)
    out.update(changes)
    return out


# ── 1. Портфель как бот ──────────────────────────────────────────────────────
def trade_stats(trades):
    r = np.array([t['pnl'] / t['risk'] for t in trades])
    if not len(r):
        return None
    win = r > 0
    eq = np.cumsum(r)
    dd = float((np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq).max())
    streak = best = 0
    for x in r:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    reasons = pd.Series([t['exit_reason'] for t in trades])
    tps = np.array([t['tps_hit'] for t in trades])
    hold = np.array([(t['exit_time'] - t['entry_time']) / np.timedelta64(1, 'h') for t in trades])
    gains, losses = r[win].sum(), -r[~win].sum()
    lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
    return {
        'n': len(r), 'win': win.mean(), 'tp1': (tps >= 1).mean(), 'tp2': (tps >= 2).mean(), 'tp3': (tps >= 3).mean(),
        'sl_clean': (reasons == 'SL').mean(), 'time': reasons.isin(['TIME_STOP', 'EOD']).mean(),
        'avg_win': r[win].mean() if win.any() else np.nan, 'avg_loss': r[~win].mean() if (~win).any() else np.nan,
        'mean': r.mean(), 'lo': lo, 'hi': hi, 'sum': r.sum(), 'pf': gains / losses if losses > 0 else np.inf,
        'dd': dd, 'streak': best, 'hold': np.median(hold),
    }


def show(label, s):
    if s is None:
        print(f'  {label:26s} сделок нет')
        return
    print(f'  {label:26s} {s["n"]:4d} сд | в плюс {s["win"]:4.0%} | цель 1 {s["tp1"]:4.0%}, 2 {s["tp2"]:4.0%}, '
          f'3 {s["tp3"]:4.0%} | чистый стоп {s["sl_clean"]:4.0%} | ср. выигрыш {s["avg_win"]:+.2f}R, '
          f'проигрыш {s["avg_loss"]:+.2f}R | {s["mean"]:+.3f}R/сд [{s["lo"]:+.3f}; {s["hi"]:+.3f}] | '
          f'итог {s["sum"]:+7.1f}R | PF {s["pf"]:.2f} | просадка {s["dd"]:5.1f}R | серия минусов {s["streak"]:2d} | '
          f'держит {s["hold"]:5.1f} ч')


def portfolio_section():
    print('1. ПОРТФЕЛЬ «КАК БОТ» — БЕЗ ФИЛЬТРА ФАНДИНГА, живые правила (конфлюенс 4.5, R:R 4, стоп 0.8%,')
    print('   предел издержек 10%, смещение 0.1%, заявка 48 ч, пауза 12 ч, кэп 3 в сторону, удержание 336 ч)')
    for pairs, name in ((ALL, 'все пары'), (POOL, '10 пар пула'), (OTHER, '10 остальных')):
        for ft, fname in ((0.0, 'касанием'), (FT, 'насквозь')):
            s_all, allt = spec(pairs=pairs, fill_through=ft), []
            print(f' {name}, налив {fname}:')
            for period in E.PERIODS:
                res, _ = E.portfolio(period, s_all, orders=orders_retry(period, s_all, LAYOUT))
                trades = sorted(res['trades'], key=lambda t: t['entry_time'])
                allt += trades
                show(period, trade_stats(trades))
            show('ВСЕГО', trade_stats(allt))
    print()


# ── 2. Заявки отдельно ───────────────────────────────────────────────────────
def setups():
    s = spec(filt=lambda r, f: True)                  # пустышка: признаки считаются у каждой заявки
    out = []
    for period in E.PERIODS:
        orders = E.orders_for(period, s)
        data = E.exec_data(period, ALL)
        prepared = {p: E._prepare(df) for p, df in data.items()}
        for ft in (0.0, FT):
            E.smc_engine.FILL_THROUGH_PCT = ft
            for o in orders:
                arr = prepared.get(o.pair)
                if arr is None:
                    continue
                start = int(np.searchsorted(arr['ts'], np.datetime64(o.created), side='right'))
                res = E.simulate_order(o, arr, start, 100.0, breakeven_after_tp1=False, max_hold_hours=336.0)
                o.meta.setdefault('res', {})[ft] = res
        E.smc_engine.FILL_THROUGH_PCT = 0.0
        for o in orders:
            row, feats, res = o.meta['row'], o.meta['feats'] or {}, o.meta['res'][0.0]
            rec = {'period': period, 'pair': o.pair, 'pool': o.pair in POOL, 'dir': int(row.dir),
                   't': pd.Timestamp(o.created, tz='UTC'), 'conf': float(row.confluence), 'rr': o.meta['rr'],
                   'stop_pct': o.meta['stop_pct'], 'touches': int(row.touches),
                   'filled': res is not None, 'r': None if res is None else res['pnl'] / res['risk'],
                   'r_ft': None if o.meta['res'][FT] is None else o.meta['res'][FT]['pnl'] / o.meta['res'][FT]['risk']}
            for name in ('liquidity_swept', 'premium_discount', 'poi_fresh', 'fvg_present', 'structure_break',
                         'ote_zone', 'killzone', 'law_of_effort'):
                rec[name] = bool(getattr(row, f'f_{name}', False))
            rec['brk'] = row.brk
            if res is not None:
                rec.update(exit=res['exit_reason'], tps=res['tps_hit'], mfe=res['mfe_r'], mae=res['mae_r'],
                           wait_h=(res['entry_time'] - o.created) / np.timedelta64(1, 'h'),
                           hold_h=(res['exit_time'] - res['entry_time']) / np.timedelta64(1, 'h'))
            for k in FEATS:
                rec[k] = feats.get(k, np.nan)
            out.append(rec)
    return pd.DataFrame(out)


def line(label, r):
    r = pd.Series(r, dtype=float).dropna()
    if len(r) < 5:
        return f'  {label:34s} {len(r):4d} сд'
    lo, hi = ci(r.to_numpy()) if len(r) > 10 else (np.nan, np.nan)
    return (f'  {label:34s} {len(r):4d} сд | в плюс {np.mean(r > 0):4.0%} | {r.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}] | '
            f'итог {r.sum():+7.1f}R')


def buckets(f, col, edges, label):
    print(f' {label}:')
    x = f[col]
    for a, b in zip(edges[:-1], edges[1:]):
        m = (x >= a) & (x < b)
        print(line(f'[{a:g}; {b:g})', f.loc[m, 'r']))
    if x.isna().any():
        print(line('нет значения', f.loc[x.isna(), 'r']))


def setup_section(f):
    filled = f[f['filled']]
    print('2. КАЖДАЯ ЗАЯВКА ОТДЕЛЬНО (без портфеля, налив касанием; «в плюс» — доля сделок с R > 0)')
    print(f'  заявок {len(f)}, налилось {len(filled)} ({len(filled) / len(f):.0%}); '
          f'ожидание налива — медиана {filled["wait_h"].median():.1f} ч')
    print(line('ВСЕ', filled['r']))
    print(line('ВСЕ, налив насквозь', f['r_ft']))
    print(' сторона:')
    for d, name in ((1, 'лонги'), (-1, 'шорты')):
        print(line(name, filled.loc[filled['dir'] == d, 'r']))
    print(' пары:')
    for pair, part in filled.groupby('pair'):
        print(line(pair + (' (пул)' if pair in POOL else ''), part['r']))
    print(' периоды:')
    for period in E.PERIODS:
        print(line(period, filled.loc[filled['period'] == period, 'r']))
    print(' полугодия:')
    half = filled['t'].map(lambda t: f'{t.year}-{"I" if t.month <= 6 else "II"}')
    for h in sorted(half.unique()):
        print(line(h, filled.loc[half == h, 'r']))
    print(' факторы ядра (да / нет):')
    for name in ('liquidity_swept', 'premium_discount', 'poi_fresh', 'fvg_present', 'structure_break',
                 'ote_zone', 'killzone', 'law_of_effort'):
        print(line(f'{name}: да', filled.loc[filled[name], 'r']))
        print(line(f'{name}: нет', filled.loc[~filled[name], 'r']))
    print(' слом структуры:')
    for b, part in filled.groupby(filled['brk'].fillna('нет')):
        print(line(str(b), part['r']))
    buckets(filled, 'conf', [4.5, 5.0, 5.5, 6.0, 6.5, 10], 'конфлюенс')
    buckets(filled, 'rr', [4, 5, 6, 8, 12, 100], 'R:R (взвешенный)')
    buckets(filled, 'stop_pct', [0.8, 1.0, 1.5, 2.0, 3.0, 5.0, 100], 'стоп, %')
    buckets(filled, 'hour', [0, 7, 12, 17, 21, 24], 'час входа-решения, UTC (Азия / Лондон / Нью-Йорк / вечер)')
    buckets(filled, 'weekday', [0, 1, 2, 3, 4, 5, 7], 'день недели (0 — пн, 5–6 — выходные)')
    buckets(filled, 'ret_7d', [-100, -10, -5, 0, 5, 10, 100], 'ход пары за 7 дней в сторону сделки, %')
    buckets(filled, 'ret_30d', [-100, -20, -10, 0, 10, 20, 100], 'ход пары за 30 дней в сторону сделки, %')
    buckets(filled, 'btc_ret_7d', [-100, -5, 0, 5, 100], 'BTC за 7 дней в сторону сделки, %')
    buckets(filled, 'ema50d_gap', [-100, -10, 0, 10, 100], 'цена к EMA50 дня в сторону сделки, %')
    buckets(filled, 'er_30d', [0, 0.1, 0.2, 0.3, 1.01], 'трендовость пары за 30 дней (0 — пила, 1 — прямая)')
    buckets(filled, 'atr_rank', [0, 25, 50, 75, 101], 'волатильность: ранг ATR за 30 дней, %')
    buckets(filled, 'stop_adr', [0, 0.25, 0.5, 0.75, 1, 100], 'стоп в долях дневного размаха')
    buckets(filled, 'dist_entry_pct', [0, 1, 2, 4, 8, 100], 'расстояние до входа при постановке, %')
    print()


def excursion_section(f):
    filled = f[f['filled']]
    lose = filled[filled['r'] <= 0]
    win = filled[filled['r'] > 0]
    print('3. ХОД ЦЕНЫ ВНУТРИ СДЕЛКИ')
    print(f'  проигравших {len(lose)}: доходили до +0.5R — {np.mean(lose["mfe"] >= 0.5):.0%}, '
          f'+1R — {np.mean(lose["mfe"] >= 1):.0%}, +2R — {np.mean(lose["mfe"] >= 2):.0%}, +3R — {np.mean(lose["mfe"] >= 3):.0%}')
    print(f'  выигравших {len(win)}: уходили против на 0.5R — {np.mean(win["mae"] >= 0.5):.0%}, '
          f'на 0.8R — {np.mean(win["mae"] >= 0.8):.0%}; в среднем {win["r"].mean():+.2f}R, '
          f'медиана хода в пользу {win["mfe"].median():.1f}R')
    print(f'  чистый стоп без единой цели: {np.mean(filled["exit"] == "SL"):.0%} сделок; '
          f'время в сделке — медиана {filled["hold_h"].median():.0f} ч')
    print()


def main():
    portfolio_section()
    f = setups()
    f.to_pickle(os.path.join(E.OUT, 'smc_stats_setups.pkl'))
    setup_section(f)
    excursion_section(f)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
