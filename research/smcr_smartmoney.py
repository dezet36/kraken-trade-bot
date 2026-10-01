"""
«Умные деньги против толпы» по метрикам Binance (docs/Умные_деньги_против_толпы_2026-10-01.md).

    python research/smcr_smartmoney.py     # H1, H2 на сетапах SMC «глазами бота»; H3 — сигнал сам по себе
"""
import os
import sys
from functools import lru_cache

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import smcr_live as L                                 # noqa: E402  (D.PERIODS['fresh'] → fresh20)
import smcr_eval as V                                 # noqa: E402
from common import ci                                 # noqa: E402

E = L.E
OUT = L.OUT
MET = os.path.join(HERE, 'binance_metrics_cache')
WIN = 2016                                             # 7 суток пятиминуток
H_MS = 3_600_000


@lru_cache(maxsize=None)
def metrics(pair):
    p = os.path.join(MET, pair + '.pkl')
    if not os.path.exists(p):
        return None
    f = pd.read_pickle(p)
    top = f['sum_toptrader_long_short_ratio']
    crowd = f['count_long_short_ratio']
    ok = (top > 0) & (crowd > 0)
    sd = np.log(top.where(ok)) - np.log(crowd.where(ok))
    cr = np.log(crowd.where(ok))
    out = pd.DataFrame(index=f.index)
    for name, s in (('sd', sd), ('cr', cr)):
        m = s.rolling(WIN, min_periods=WIN // 2).mean().shift(1)
        sdv = s.rolling(WIN, min_periods=WIN // 2).std().shift(1)
        out[name + 'z'] = (s - m) / sdv
    ms = ((out.index - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta(milliseconds=1)).to_numpy('int64')
    return ms, out['sdz'].to_numpy(float), out['crz'].to_numpy(float)


def feat_at(pair, t_ms):
    """SDz и CRz по последней пятиминутке, закрытой до момента решения."""
    got = metrics(pair)
    if got is None:
        return np.nan, np.nan
    ms, sdz, crz = got
    k = int(np.searchsorted(ms, t_ms - 5 * 60_000, side='right')) - 1   # строка с меткой ≤ t − 5 мин
    if k < 0 or t_ms - ms[k] > 30 * 60_000:
        return np.nan, np.nan                                             # старше 30 мин — прочерк
    return sdz[k], crz[k]


# ── H1, H2: фильтры на сетапах SMC ───────────────────────────────────────────
def filters():
    f = pd.read_pickle(os.path.join(OUT, 'live_orders.pkl'))
    f = f[f['sample'].isin(['pool', 'other'])].reset_index(drop=True)
    vals = [feat_at(p, int(t)) for p, t in zip(f['pair'], f['t'])]
    f['sdz'] = [v[0] for v in vals]
    f['crz'] = [v[1] for v in vals]
    print(f'покрытие признаков: SDz есть у {100 * f["sdz"].notna().mean():.0f}% заявок '
          f'(по периодам: ' + ', '.join(f'{p} {100 * g["sdz"].notna().mean():.0f}%' for p, g in f.groupby('period')) + ')')
    rules = {'H1 по стороне умных денег (SDz)': lambda d: d['sdz'] * d['dir'] > 0,
             'H2 против толпы по счетам (CRz)': lambda d: d['crz'] * d['dir'] < 0}
    for label, sel in (('ядро без фильтра фандинга', lambda d: d),
                       ('живая SMC (фильтр фандинга)', lambda d: d[d['crowd'] & d['fund_bp'].notna()])):
        print(f'\nФИЛЬТРЫ — {label}')
        for name, rule in rules.items():
            for part_name, part in (('отбор 2022–24', lambda d: d[V.early_mask(d)]),
                                    ('приёмка 2024–26', lambda d: d[V.late_mask(d)])):
                g = sel(part(f[f['sample'] == 'pool'])).dropna(subset=['sdz', 'crz']).reset_index(drop=True)
                m = rule(g).to_numpy()
                eff, p, n = V.placebo(g, m)
                eff_ft, _, _ = V.placebo(g, m, col='r_ft')
                print(f'  {name:34s} {part_name}: {n:4d} из {len(g):4d} заявок, эффект {eff:+.3f}, p {p:.3f}, '
                      f'насквозь {eff_ft:+.3f}', flush=True)
            o = sel(f[f['sample'] == 'other']).dropna(subset=['sdz', 'crz']).reset_index(drop=True)
            mo = rule(o).to_numpy()
            eff, p, n = V.placebo(o, mo)
            print(f'  {name:34s} 10 пар вне пула: {n:4d} из {len(o):4d}, эффект {eff:+.3f}, p {p:.3f}', flush=True)
    # H2 вместо фильтра фандинга: тот же объём «против толпы», но по счетам Binance
    pool = f[f['sample'] == 'pool'].dropna(subset=['crz'])
    fund = pool[pool['crowd'] & pool['fund_bp'].notna()]
    h2 = pool[pool['crz'] * pool['dir'] < 0]
    print('\nH2 ВМЕСТО фильтра фандинга (10 пар пула; R на сделку, итог) — фандинг против счетов:')
    for p in V.PERIODS5:
        a, b = fund[fund['period'] == p]['r'].dropna(), h2[h2['period'] == p]['r'].dropna()
        print(f'  {p:5s} фандинг {len(a):4d} сд {a.mean() if len(a) else 0:+.3f} {a.sum():+7.1f}R | '
              f'счета {len(b):4d} сд {b.mean() if len(b) else 0:+.3f} {b.sum():+7.1f}R')


# ── H3: расхождение как самостоятельный сигнал ──────────────────────────────
def standalone():
    import backtest_smc as bt
    pool10 = L.POOL10
    other10 = tuple(p for p in L.D.PAIRS if p not in pool10)
    rows = []
    for period in ('bear', 'mid1', 'mid2', '12m', 'fresh'):
        bt.CACHE_DIR = os.path.join(HERE, L.D.PERIODS[period])
        data5 = E.exec_data(period, L.D.PAIRS)
        for pair in L.D.PAIRS:
            got = metrics(pair)
            df1 = bt.load_cached(pair, '1h')
            if got is None or df1 is None or pair not in data5:
                continue
            arr = E._prepare(data5[pair])
            h, l, c = (df1[k].to_numpy(float) for k in ('high', 'low', 'close'))
            prev = np.concatenate([[c[0]], c[:-1]])
            atr = pd.Series(np.maximum(h - l, np.maximum(abs(h - prev), abs(l - prev)))).rolling(14).mean().to_numpy()
            t_close = ((pd.to_datetime(df1['timestamp'], utc=True) - pd.Timestamp('1970-01-01', tz='UTC'))
                       // pd.Timedelta(milliseconds=1)).to_numpy('int64') + H_MS
            last_sig = -10 ** 18
            prev_z = np.nan
            for i in range(30, len(df1)):
                z, _ = feat_at(pair, int(t_close[i]))
                fire = np.isfinite(z) and abs(z) > 2 and not (np.isfinite(prev_z) and abs(prev_z) > 2)
                prev_z = z
                if not fire or t_close[i] - last_sig < 24 * H_MS or not np.isfinite(atr[i]):
                    continue
                last_sig = t_close[i]
                side = 1 if z > 0 else -1
                created = np.datetime64(int(t_close[i]) - 1, 'ms')
                k = int(np.searchsorted(arr['ts'], created, side='right'))
                if k >= len(arr['ts']):
                    continue
                entry = float(arr['open'][k])
                stop = entry - side * 2 * atr[i]
                tgt = entry + side * 4 * atr[i]
                o = E.Order(pair=pair, direction='BULLISH' if side > 0 else 'BEARISH', entry=entry, stop=stop,
                            targets=[tgt], fractions=[1.0], created=created,
                            expires=created + np.timedelta64(3600, 's'), key=(pair, i), entry_type='stop')
                res = E.simulate_order(o, arr, k, 100.0, breakeven_after_tp1=False, max_hold_hours=48.0)
                if res is None:
                    continue
                rows.append({'period': period, 'pair': pair, 't': int(t_close[i]), 'dir': side,
                             'sample': 'pool' if pair in pool10 else 'other', 'r': res['pnl'] / res['risk']})
        E._exec.pop(period, None)
    f = pd.DataFrame(rows)
    f.to_pickle(os.path.join(OUT, 'smartmoney_h3.pkl'))
    print('\nH3 — крайнее расхождение умных и толпы (|SDz| > 2), вход по стороне умных денег:')
    for sample in ('pool', 'other'):
        g = f[f['sample'] == sample]
        for name, part in (('отбор 2022–24', g[V.early_mask(g)]), ('приёмка 2024–26', g[V.late_mask(g)])):
            r = part['r'].to_numpy(float)
            lo, hi = ci(r) if len(r) > 10 else (np.nan, np.nan)
            print(f'  {"10 пар пула" if sample == "pool" else "10 пар вне пула":15s} {name}: {len(r):4d} сд '
                  f'{r.mean() if len(r) else 0:+.3f} [{lo:+.3f}; {hi:+.3f}] итог {r.sum():+.1f}R; '
                  f'лонги {part[part.dir > 0]["r"].mean():+.3f}, шорты {part[part.dir < 0]["r"].mean():+.3f}', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    filters()
    standalone()
