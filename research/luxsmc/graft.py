"""
Что взять из LuxAlgo в живую SMC (PROTOCOL_GRAFT.md, записан до расчёта):
X1 волатильный блок, X2/X3 тренды LuxAlgo 1ч, X4 premium/discount LuxAlgo,
X5 цель — имбаланс 4ч. Стенд SMC «глазами бота» (smc_lab_eval, раскладки
'live' и 'live20'), фильтр толпы с порогом 0, фандинг по факту.

    python research/luxsmc/graft.py        # → results/luxsmc_graft/report.txt
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
sys.path.insert(0, RESEARCH)
sys.path.insert(0, HERE)

import smcr_live as SL           # noqa: E402  (fresh → кэш 20 пар, BAR_H для 'live'/'live20')
import lux                       # noqa: E402

E = SL.E
OUT = os.path.join(RESEARCH, 'results', 'luxsmc_graft')
PERIODS = ['bear', 'mid1', 'mid2', '12m', 'fresh']
SELECT, ACCEPT = ('bear', 'mid1'), ('mid2', '12m', 'fresh')
H = 3_600_000
FT = 0.0005


def against(f, limit=0.0):
    """Фильтр толпы бота: ставка в сторону сделки ≤ порога; нет ставки — пропуск."""
    x = f.get('funding', np.nan)
    return not (x > limit)


# ── Разметка LuxAlgo по часовым свечам периода ──────────────────────────────

@lru_cache(maxsize=None)
def lux_state(period, pair):
    c = SL.candles_1h(period, pair)
    if c is None:
        return None
    o, h, l, cl = (c[k].to_numpy(np.float64) for k in ('open', 'high', 'low', 'close'))
    t_open = pd.DatetimeIndex(c['timestamp']).as_unit('ms').asi8      # мс открытия часа
    n = len(cl)
    piv = []
    ev = pd.DataFrame(lux.build(h, l, cl, pivots=piv))
    bias = {}
    for scope in ('swing', 'internal'):
        x = ev[ev.scope == scope] if len(ev) else ev
        if not len(x):
            bias[scope] = np.zeros(n, np.int64)
            continue
        k = np.searchsorted(x.i.to_numpy(), np.arange(n), side='right') - 1
        bias[scope] = np.where(k >= 0, x.side.to_numpy()[np.clip(k, 0, None)], 0)
    # крайние точки: max/min с последней свинговой точки (updateTrailingExtremes)
    by_i = {}
    for p in piv:
        if p['scope'] == 'swing':
            by_i.setdefault(p['i'], []).append(p)
    top = bot = np.nan
    tops, bots = np.full(n, np.nan), np.full(n, np.nan)
    for i in range(n):
        top = max(h[i], top) if top == top else np.nan
        bot = min(l[i], bot) if bot == bot else np.nan
        for p in by_i.get(i, ()):
            if p['kind'] == 1:
                top = p['level']
            else:
                bot = p['level']
        tops[i], bots[i] = top, bot
    return {'h': h, 'l': l, 't_open': t_open, 'atr': lux.atr_rma(h, l, cl, 200),
            'swing': bias['swing'], 'internal': bias['internal'], 'top': tops, 'bot': bots,
            'fvg': fvg_targets(t_open, o, h, l, cl)}


def fvg_targets(t_open, o, h, l, c):
    """Имбалансы 4ч (LuxAlgo) из часовых свечей: по стороне сделки массив
    (уровень, известен с, снят с) в мс. Цель лонга — нижний край медвежьего
    гэпа, шорта — верхний край бычьего; снят — первый час, коснувшийся края."""
    key = t_open // (4 * H)
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(key)]
    full = (ends - starts) == 4
    s, e = starts[full], ends[full]
    O, C = o[s], c[e - 1]
    Hh = np.maximum.reduceat(h, starts)[full]
    Ll = np.minimum.reduceat(l, starts)[full]
    T = key[s] * 4 * H + 4 * H                          # закрытие бара 4ч
    out = {1: [], -1: []}
    delta = np.full(len(C), np.nan)
    delta[1:] = (C[:-1] - O[:-1]) / (O[:-1] * 100)
    cum = np.nancumsum(np.abs(np.nan_to_num(delta)))
    for i in range(2, len(C)):
        thr = cum[i] / i * 2
        if Ll[i] > Hh[i - 2] and C[i - 1] > Hh[i - 2] and delta[i] > thr:
            out[-1].append((Ll[i], T[i]))               # бычий гэп: цель шорта — верхний край
        if Hh[i] < Ll[i - 2] and C[i - 1] < Ll[i - 2] and -delta[i] > thr:
            out[1].append((Hh[i], T[i]))                # медвежий гэп: цель лонга — нижний край
    res = {}
    for side, items in out.items():
        rows = []
        for level, known in items:
            j0 = int(np.searchsorted(t_open, known))
            seg = h[j0:] >= level if side == 1 else l[j0:] <= level
            k = np.flatnonzero(seg)
            gone = t_open[j0 + k[0]] + H if len(k) else np.iinfo(np.int64).max
            rows.append((level, known, gone))
        res[side] = np.array(rows, dtype=np.float64).reshape(-1, 3)
    return res


# ── Варианты ────────────────────────────────────────────────────────────────

def _x1(r):
    L = lux_state(r.period, r.pair)
    k = int(r.poi_index)
    if L is None or not 0 <= k < len(L['h']) or np.isnan(L['atr'][k]):
        return True
    return (L['h'][k] - L['l'][k]) < 2 * L['atr'][k]


def _trend(scope):
    def fn(r):
        L = lux_state(r.period, r.pair)
        if L is None or not 0 <= int(r.i) < len(L['h']):
            return True
        b = L[scope][int(r.i)]
        return b == 0 or b == r.dir
    return fn


def _x4(r):
    L = lux_state(r.period, r.pair)
    if L is None or not 0 <= int(r.i) < len(L['h']):
        return True
    mid = (L['top'][int(r.i)] + L['bot'][int(r.i)]) / 2
    if np.isnan(mid):
        return True
    return r.entry < mid if r.dir > 0 else r.entry > mid


FILTERS = {'base': None, 'X1': _x1, 'X2': _trend('swing'), 'X3': _trend('internal'), 'X4': _x4, 'X5': None}
NAMES = {'base': 'живая SMC (толпа ≤ 0)', 'X1': 'X1 волатильный блок не берётся',
         'X2': 'X2 по тренду свинга LuxAlgo 1ч', 'X3': 'X3 по внутреннему тренду LuxAlgo 1ч',
         'X4': 'X4 premium/discount LuxAlgo', 'X5': 'X5 цель — имбаланс 4ч'}


def spec(variant, pairs, layout, ft):
    extra = FILTERS[variant]
    filt = (lambda r, f: against(f)) if extra is None else (lambda r, f: against(f) and extra(r))
    return dict(E.LIVE, pairs=tuple(pairs), layouts=(layout,), filt=filt, fill_through=ft)


def retarget(period, orders):
    """X5: вся позиция — на ближайшем нетронутом имбалансе 4ч не ближе 1R."""
    out = []
    for o in orders:
        L = lux_state(period, o.pair)
        if L is None:
            continue
        d = 1 if o.direction == 'BULLISH' else -1
        t = int(o.meta['row'].t)
        risk = abs(o.entry - o.stop)
        c = L['fvg'][d]
        if not len(c):
            continue
        ok = (c[:, 1] <= t) & (c[:, 2] > t) & ((c[:, 0] - o.entry) * d >= risk)
        if not ok.any():
            continue
        lv = c[ok, 0][np.argmin(np.abs(c[ok, 0] - o.entry))]
        out.append(E.Order(pair=o.pair, direction=o.direction, entry=o.entry, stop=o.stop,
                           targets=[float(lv)], fractions=[1.0], created=o.created, expires=o.expires,
                           key=o.key, meta=o.meta))
    return out


def run_period(period, variant, pairs, layout, ft):
    sp = spec(variant, pairs, layout, ft)
    orders = E.orders_for(period, sp)
    if variant == 'X5':
        orders = retarget(period, orders)
    res, _ = E.portfolio(period, sp, orders)
    out = []
    for t in res['trades']:
        if period == 'fresh' and E.overlaps_12m(t):
            continue                                    # уже посчитана в 12m
        out.append(t['pnl'] / t['risk'])
    return np.array(out)


def main():
    os.makedirs(OUT, exist_ok=True)
    pool = tuple(E.POOL10)
    other = tuple(sorted(set(E.rows('bear', 'live20').pair)))
    runs = [('pool', pool, 'live', 0.0), ('pool_ft', pool, 'live', FT), ('other', other, 'live20', 0.0)]
    res = {}
    for period in PERIODS:
        for sample, pairs, layout, ft in runs:
            for v in FILTERS:
                res[(sample, v, period)] = run_period(period, v, pairs, layout, ft)
            print(f'{period} {sample}: ' + ' '.join(f'{v} {len(res[(sample, v, period)])}/'
                                                    f'{res[(sample, v, period)].sum():+.1f}' for v in FILTERS),
                  flush=True)
        E._exec.pop(period, None)
        SL.simulate.cache.pop(period, None)
        lux_state.cache_clear()

    def agg(sample, v, periods):
        r = np.concatenate([res[(sample, v, p)] for p in periods])
        return len(r), r.sum(), (r.mean() if len(r) else np.nan)

    lines = ['вариант | отбор bear+mid1: сделок, итог, R/сд | приёмка mid2+12m+fresh | насквозь | вне пула (приёмка) | по периодам',
             '']
    base = {k: agg(*k) for k in [('pool', 'base', SELECT), ('pool', 'base', ACCEPT),
                                  ('pool_ft', 'base', ACCEPT), ('other', 'base', ACCEPT)]}
    for v in FILTERS:
        s = agg('pool', v, SELECT)
        a = agg('pool', v, ACCEPT)
        af = agg('pool_ft', v, ACCEPT)
        ot = agg('other', v, ACCEPT)
        per = ' '.join(f'{p} {res[("pool", v, p)].sum():+.1f}' for p in PERIODS)
        lines.append(f'{NAMES[v]:38s} | {s[0]:4d} {s[1]:+7.1f} {s[2]:+.3f} | {a[0]:4d} {a[1]:+7.1f} {a[2]:+.3f} | '
                     f'{af[1]:+7.1f} {af[2]:+.3f} | {ot[0]:4d} {ot[2]:+.3f} | {per}')
        if v == 'base':
            continue
        sel = s[1] > base[('pool', 'base', SELECT)][1] and s[2] > base[('pool', 'base', SELECT)][2]
        c1 = a[1] > base[('pool', 'base', ACCEPT)][1] and a[2] > base[('pool', 'base', ACCEPT)][2]
        c2 = af[1] > base[('pool_ft', 'base', ACCEPT)][1] and af[2] > base[('pool_ft', 'base', ACCEPT)][2]
        c3 = ot[2] >= base[('other', 'base', ACCEPT)][2]
        verdict = ('ПРИНЯТ' if sel and c1 and c2 and c3 else
                   'не прошёл отбор' if not sel else 'не принят')
        lines.append(f'{"":38s}   отбор {"да" if sel else "нет"} · приёмка {"да" if c1 else "нет"} · '
                     f'насквозь {"да" if c2 else "нет"} · вне пула {"да" if c3 else "нет"} → {verdict}')
    text = '\n'.join(lines)
    with open(os.path.join(OUT, 'report.txt'), 'w', encoding='utf-8') as fh:
        fh.write(text + '\n')
    print(text)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
