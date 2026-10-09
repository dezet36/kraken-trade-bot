"""
Старая ФИБО: находки разбора живых сделок (Р1–Р6) на истории «как в боте».
Протокол — docs/ФИБО_разбор_сделок_2026-10-08.md (закоммичен до проверки, 4d6391a).

    Р1  медленный откат: заявка не раньше 3.5 ч после конца импульса, если цена
        к этому моменту не касалась входа и цели и стоит в окне сканера;
    Р2  сила тренда 4ч ≤ 0.06;   Р3  заявка не в 00–05 UTC;
    Р4  ATR(14) по 5-минуткам ≥ 0.37% цены (как atr_pct журнала бота);   Р5  объём 24 ч ≥ 0.81 среднего за 14 сут;
    Р6  держится ли результат пар от периода к периоду.

    python research/fibo_live_findings.py   → results/fibo_live_findings.txt
"""
import copy
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fibo_live_sim as FS                            # noqa: E402
import ai_setups as S                                 # noqa: E402
import backtest_smc as bt                             # noqa: E402
from infra import config  # noqa: E402
import smc_engine                                     # noqa: E402
from common import ci                                 # noqa: E402
from fibo_crowd import funding_at                     # noqa: E402
from fibo_geometry import CELLS, impulse, load_cell  # noqa: E402
from fibo_htf_split import BASE, N4                   # noqa: E402

H = np.timedelta64(1, 'h')
DELAY = np.timedelta64(210, 'm')                      # Р1: 3.5 ч
TH = dict(htf=0.06, atr=0.37, vol=0.81)               # пороги — из живых сделок


def naive(x):
    t = pd.Timestamp(x)
    return np.datetime64(t.tz_convert('UTC').tz_localize(None) if t.tzinfo else t)


def wilder_atr(h, l, c, n=14):
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    tr = np.r_[h[0] - l[0], tr]
    a = np.empty(len(tr))
    a[0] = tr[0]
    for i in range(1, len(tr)):
        a[i] = (a[i - 1] * i + tr[i]) / (i + 1) if i < n else (a[i - 1] * (n - 1) + tr[i]) / n
    return a


def prepare(cell):
    """Признаки заявок и заявки Р1 (отложенные) по ячейке."""
    cache, orders, pairs = load_cell(cell)
    orders = [o for o in orders if o.meta['cost_share'] <= FS.COST_LIMIT]
    bt.CACHE_DIR = os.path.join(HERE, cache)
    fund = {p: S.load_series(cache, 'funding', p, 'funding_rate') for p in pairs}
    feat, delayed, no_end = {}, {}, 0
    af, as_ = 2 / (config.HTF_EMA_FAST + 1), 2 / (config.HTF_EMA_SLOW + 1)
    for p in pairs:
        own = [(i, o) for i, o in enumerate(orders) if o.pair == p]
        if not own:
            continue
        data = bt.load_pair(p)
        h1, m5, h4 = data['1h'], data['5m'], data['4h']
        t1 = FS.naive(h1['timestamp'])
        hi1, lo1, c1, v1 = (h1[c].to_numpy(float) for c in ('high', 'low', 'close', 'volume'))
        atr5 = None
        t5 = FS.naive(m5['timestamp'])
        hi5, lo5, c5 = (m5[c].to_numpy(float) for c in ('high', 'low', 'close'))
        t4 = FS.naive(h4['timestamp'])
        c4 = h4['close'].to_numpy(float)
        for i, o in own:
            T = naive(o.created)
            d = 1 if o.direction == 'LONG' else -1
            x = {'crowd': bool(funding_at(fund.get(p), int(pd.Timestamp(T).value // 10 ** 6)) * d <= 0),
                 'hour': int(pd.Timestamp(T).hour)}
            kt = int(np.searchsorted(t1 + H, T, side='right'))            # закрытых часов к T
            if atr5 is None:
                atr5 = wilder_atr(hi5, lo5, c5)
            j5a = int(np.searchsorted(t5, T, side='left'))
            x['atr'] = atr5[j5a - 1] / c5[j5a - 1] * 100 if j5a > 0 else np.nan
            x['vol'] = v1[max(0, kt - 24):kt].mean() / v1[max(0, kt - 336):kt].mean() if kt > 336 else np.nan
            # сила тренда 4ч — как get_htf_trend: закрытые 4ч + формирующаяся
            hour = np.datetime64(pd.Timestamp(T - np.timedelta64(1, 'm')).floor('h'))
            k4 = int(np.searchsorted(t4, hour, side='right')) - 1
            j5 = int(np.searchsorted(t5, T, side='left'))
            closes = np.append(c4[max(0, k4 - N4 + 1):k4], c5[j5 - 1]) if k4 > 0 and j5 > 0 else np.array([])
            if len(closes) >= config.HTF_EMA_SLOW + 10:
                ef = pd.Series(closes).ewm(alpha=af, adjust=False).mean().iloc[-1]
                es = pd.Series(closes).ewm(alpha=as_, adjust=False).mean().iloc[-1]
                x['htf'] = abs(ef - es) / es
            else:
                x['htf'] = 0.0
            feat[i] = x
            # Р1: конец импульса — последний закрытый час с экстремумом = концу ноги
            b, s, _, _ = impulse(o)
            ext = hi1 if d == 1 else lo1
            e_idx = next((m for m in range(kt - 1, max(-1, kt - 61), -1)
                          if abs(ext[m] - b) <= 1e-9 * abs(b)), None)
            if e_idx is None:
                no_end += 1                        # конец импульса — в формирующемся часе
                e_end = t1[kt] + H if kt < len(t1) else T
            else:
                e_end = t1[e_idx] + H
            start = e_end + DELAY
            if start <= T:
                delayed[i] = o
                continue
            if start >= naive(o.expires):
                continue
            a5, b5 = int(np.searchsorted(t5, T, side='left')), int(np.searchsorted(t5, start, side='left'))
            tgt = o.targets[0]
            touched = ((lo5[a5:b5] <= o.entry).any() or (hi5[a5:b5] >= tgt).any()) if d == 1 else \
                      ((hi5[a5:b5] >= o.entry).any() or (lo5[a5:b5] <= tgt).any())
            if touched or b5 < 1:
                continue
            price = c5[b5 - 1]
            lvl382 = b - d * 0.382 * s
            if not (min(lvl382, b) <= price <= max(lvl382, b)):
                continue
            c = copy.copy(o)
            c.created = start
            delayed[i] = c
    return cache, orders, pairs, feat, delayed, no_end


def run_cell(cell):
    cache, orders, pairs, feat, delayed, no_end = prepare(cell)
    exec_data = {p: d['5m'] for p in pairs if (d := bt.load_pair(p)) is not None}
    f3 = lambda i, o: o.direction == 'SHORT' and feat[i]['crowd']           # noqa: E731
    rules = [
        ('база: F3 (как в боте)', lambda i, o: f3(i, o), False),
        ('Р1 медленный откат (заявка ≥ 3.5 ч после импульса)', lambda i, o: f3(i, o), True),
        ('Р2 сила тренда 4ч ≤ 0.06', lambda i, o: f3(i, o) and feat[i]['htf'] <= TH['htf'], False),
        ('Р3 не ночью (заявка не в 00–05 UTC)', lambda i, o: f3(i, o) and feat[i]['hour'] >= 6, False),
        ('Р4 ATR 5м ≥ 0.37%', lambda i, o: f3(i, o) and feat[i]['atr'] >= TH['atr'], False),
        ('Р5 объём 24 ч ≥ 0.81 среднего', lambda i, o: f3(i, o) and feat[i]['vol'] >= TH['vol'], False),
        ('база: обе стороны', lambda i, o: True, False),
        ('(Р1 на обе)', lambda i, o: True, True),
        ('(Р3 на обе)', lambda i, o: feat[i]['hour'] >= 6, False),
        ('(Р4 на обе)', lambda i, o: feat[i]['atr'] >= TH['atr'], False),
    ]
    out, trades = {}, {}
    for name, rule, use_delay in rules:
        if use_delay:
            pool = [delayed[i] for i, o in enumerate(orders) if i in delayed and rule(i, o)]
        else:
            pool = [o for i, o in enumerate(orders) if rule(i, o)]
        for through in (0.0, 0.0005):
            smc_engine.FILL_THROUGH_PCT = through
            try:
                res = smc_engine.run_portfolio(pool, exec_data, **BASE)
            finally:
                smc_engine.FILL_THROUGH_PCT = 0.0
            out[(name, through)] = np.array([t['pnl'] / t['risk'] for t in res['trades']])
            if through == 0.0 and name.startswith('база: F3'):
                trades = pd.DataFrame({'pair': [t['pair'] for t in res['trades']],
                                       'r': out[(name, 0.0)]})
    kept = sum(1 for i, o in enumerate(orders) if f3(i, o) and i in delayed)
    print(f'   {cell}: заявок F3 {sum(1 for i, o in enumerate(orders) if f3(i, o))}, после Р1 {kept}; '
          f'конец импульса не найден {no_end}', flush=True)
    return cell, out, trades, [n for n, _, _ in rules]


def mean(a):
    return float(a.mean()) if len(a) else float('nan')


def main():
    with Pool(min(len(CELLS), 5)) as pool:
        got = list(pool.imap_unordered(run_cell, CELLS))
    res = {c: o for c, o, _, _ in got}
    trades = {c: t for c, _, t, _ in got}
    names = got[0][3]
    print('\nR на сделку по ячейкам (касание), затем всё вместе: касание [95%] / насквозь')
    print(f'{"":52s} ' + ' '.join(f'{g[:3]}-{p:4s}' for g, p in CELLS))
    for n in names:
        a = np.concatenate([res[c][(n, 0.0)] for c in CELLS])
        t = np.concatenate([res[c][(n, 0.0005)] for c in CELLS])
        lo, hi = ci(a)
        print(f'{n:52s} ' + ' '.join(f'{mean(res[c][(n, 0.0)]):+.3f}' for c in CELLS)
              + f'\n{"":52s} сделок {len(a)}, {mean(a):+.3f} [{lo:+.3f}; {hi:+.3f}] | насквозь {mean(t):+.3f}')
    print('\nПРИЁМКА (против F3: 8 из 9 ячеек лучше; насквозь > 0; нижняя граница > 0)')
    base = 'база: F3 (как в боте)'
    for n in names[1:6]:
        better = sum(mean(res[c][(n, 0.0)]) > mean(res[c][(base, 0.0)]) for c in CELLS)
        a = np.concatenate([res[c][(n, 0.0)] for c in CELLS])
        t = np.concatenate([res[c][(n, 0.0005)] for c in CELLS])
        lo, _ = ci(a)
        ok = better >= 8 and mean(t) > 0 and lo > 0
        print(f'  {n:52s} лучше в {better}/9; насквозь {mean(t):+.3f}; нижняя {lo:+.3f} → {"ПРИНЯТА" if ok else "не принята"}')
    # Р6: держится ли результат пар
    early = pd.concat([trades[c] for c in CELLS if c[1] in ('bear', 'mid1')])
    late = pd.concat([trades[c] for c in CELLS if c[1] in ('mid2', '12m')])
    a = early.groupby('pair').r.mean()
    b = late.groupby('pair').r.mean()
    j = pd.concat([a.rename('2022–24'), b.rename('2024–26')], axis=1).dropna()
    rho = j['2022–24'].rank().corr(j['2024–26'].rank())
    print(f'\nР6 пары (F3): ранговая связь R пар 2022–24 и 2024–26 = {rho:+.2f} по {len(j)} парам')
    print(j.round(3).sort_values('2022–24').to_string())


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
