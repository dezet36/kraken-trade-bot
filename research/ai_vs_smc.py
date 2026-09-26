"""
Почему SMC в плюсе, а ИИ в минусе: сетапы SMC, в которых по одной детали
заменены на правила ИИ.

ВОПРОС ВЛАДЕЛЬЦА 26.09.2026: разделы ИИ и SMC в боте построены на одних и тех
же понятиях (структура, ордер-блоки, имбалансы, ликвидность) — почему голая
SMC в плюсе, а ИИ в минусе? Словарь и данные у них общие, решения — разные.
Здесь берутся ровно сетапы SMC (боевое ядро, backtest_smc.smc_orders) и в
них по одному подменяются решения ИИ:

    стоп ИИ        — за последним HL/LH часа + отступ охоты 0.3% + 0.1·ATR,
                     теснее max(1.5%, ½ATR) — сетап не берётся (минимум ИИ);
    цели ИИ        — ближняя нетронутая ликвидность (свинги 1ч/4ч, вчерашний
                     экстремум) с R:R ≥ 2 и следующая за ней, 50/50, первая —
                     не дальше дневного размаха (ворота ИИ);
    сопровождение  — безубыток при +1R и после первой цели, снятие заявки у
                     цели, заявка живёт 12 ч вместо 48.

Сравнение — на одних и тех же сетапах (тех, для которых правила ИИ дают стоп и
цель), каждая заявка отдельно, без портфеля.

Запуск:
    python research/ai_vs_smc.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'Live_Bot'))

import logger                                          # noqa: E402
logger.log = lambda *a, **k: None

import ai_doctrine as D                               # noqa: E402
import backtest_smc as bt                             # noqa: E402
from common import ci                                 # noqa: E402
from smc import params as smc_params                  # noqa: E402
from smc_engine import Order, _prepare, simulate_order  # noqa: E402

H = 3_600_000


def snapshots(d, need):
    """Последний HL/LH часа и нетронутая ликвидность на нужных часовых свечах — как у доктрины."""
    a1, a4, ad = d['a1'], d['a4'], d['ad']
    t1, t4, td = D.Tracker(a1), D.Tracker(a4), D.Tracker(ad)
    liq = D.Liquidity()
    out, j4, jd = {}, -1, -1
    for i in range(len(a1)):
        t_close = a1[i, 0] + H
        t1.advance(i)
        for p in t1.pending:
            liq.add(p, a1[p['index'] + 1:i + 1, 2].max() if p['index'] < i else -np.inf,
                    a1[p['index'] + 1:i + 1, 3].min() if p['index'] < i else np.inf)
        while j4 + 1 < len(a4) and a4[j4 + 1, 0] + 4 * H <= t_close:
            j4 += 1
            t4.advance(j4)
            for p in t4.pending:
                since = int(np.searchsorted(a1[:, 0], a4[p['index'], 0] + 4 * H))
                liq.add(p, a1[since:i + 1, 2].max() if since <= i else -np.inf,
                        a1[since:i + 1, 3].min() if since <= i else np.inf)
        while jd + 1 < len(ad) and ad[jd + 1, 0] + 24 * H <= t_close:
            jd += 1
            td.advance(jd)
        liq.take(a1[i, 2], a1[i, 3])
        if i in need:
            out[i] = {'HL': t1.last['HL'], 'LH': t1.last['LH'], 'highs': sorted(liq.highs),
                      'lows': sorted(liq.lows), 'pdh': ad[jd, 2] if jd >= 0 else None,
                      'pdl': ad[jd, 3] if jd >= 0 else None, 'atr': d['atr1'][i], 'adr': d['adr'][i]}
    return out


def ai_stop(snap, long_, entry):
    ref = snap['HL' if long_ else 'LH']
    if ref is None:
        return None
    a = snap['atr'] if np.isfinite(snap['atr']) else 0.0
    buf = (D.STOP_HUNT_BASE_PCT + D.STOP_HUNT_ATR_SHARE * a) / 100
    stop = ref['price'] * (1 - buf) if long_ else ref['price'] * (1 + buf)
    if (stop >= entry) if long_ else (stop <= entry):
        return None
    if abs(entry - stop) / entry * 100 < max(D.MIN_STOP_COST_PCT, D.STOP_ATR_SHARE * a):
        return None
    return stop


def ai_targets(snap, long_, entry, stop):
    risk = abs(entry - stop)
    pool = list(snap['highs'] if long_ else snap['lows'])
    extra = snap['pdh'] if long_ else snap['pdl']
    if extra is not None:
        pool.append(extra)
    ahead = sorted({p for p in pool if (p > entry if long_ else p < entry)}, reverse=not long_)
    ok = [p for p in ahead if abs(p - entry) / risk >= D.MIN_RR]
    if not ok:
        return None
    tp1 = ok[0]
    if np.isfinite(snap['adr']) and abs(tp1 - entry) / entry * 100 > snap['adr']:
        return None
    rest = [p for p in ahead if (p > tp1 if long_ else p < tp1)]
    return [tp1] + rest[:1]


def variant(o, stop=None, targets=None, fractions=None, be_1r=False, ttl_h=None):
    stop = o.stop if stop is None else stop
    long_ = o.direction in ('BULLISH', 'LONG')
    risk = abs(o.entry - stop)
    expires = o.expires if ttl_h is None else o.created + np.timedelta64(int(ttl_h * 3600), 's')
    return Order(pair=o.pair, direction=o.direction, entry=o.entry, stop=stop,
                 targets=list(o.targets if targets is None else targets),
                 fractions=list(o.fractions if fractions is None else fractions),
                 created=o.created, expires=expires, key=o.key, meta=o.meta,
                 be_trigger=(o.entry + risk if long_ else o.entry - risk) if be_1r else None)


def main():
    names = ['SMC как есть', 'стоп ИИ', 'цели ИИ (50/50)', 'сопровождение ИИ', 'срок заявки 12 ч', 'всё как у ИИ']
    totals = {n: [] for n in names}
    base_all = []
    for period, cache in D.PERIODS.items():
        bt.CACHE_DIR = os.path.join(HERE, cache)
        rows = {n: [] for n in names}
        skipped = {'нет стопа ИИ': 0, 'нет цели ИИ': 0}
        for pair in bt.DEFAULT_PAIRS:
            frames = bt.load_pair(pair)
            d = D.prepare(cache, pair)
            if frames is None or d is None:
                continue
            orders = bt.smc_orders(pair, frames)
            idx = {}
            for o in orders:
                t_ms = int(pd.Timestamp(o.created).value // 10 ** 6)
                idx[o.key] = int(np.searchsorted(d['a1'][:, 0], t_ms - H, side='right')) - 1
            snaps = snapshots(d, set(idx.values()))
            ex = _prepare(frames['5m'])

            def run(order, be_after_tp1=False, cancel=False):
                start = int(np.searchsorted(ex['ts'], order.created))
                res = simulate_order(order, ex, start, 100.0, breakeven_after_tp1=be_after_tp1,
                                     max_hold_hours=smc_params.MAX_POSITION_HOLD_HOURS, cancel_at_target=cancel)
                return None if res is None else res['pnl'] / res['risk']

            for o in orders:
                base = run(o)
                if base is not None:
                    base_all.append(base)
                snap = snaps.get(idx[o.key])
                if snap is None:
                    continue
                long_ = o.direction in ('BULLISH', 'LONG')
                s_ai = ai_stop(snap, long_, o.entry)
                if s_ai is None:
                    skipped['нет стопа ИИ'] += 1
                    continue
                t_smc_stop = ai_targets(snap, long_, o.entry, o.stop)
                t_ai_stop = ai_targets(snap, long_, o.entry, s_ai)
                if t_smc_stop is None or t_ai_stop is None:
                    skipped['нет цели ИИ'] += 1
                    continue
                fr = lambda t: [0.5, 0.5] if len(t) == 2 else [1.0]   # noqa: E731
                got = {
                    'SMC как есть': base,
                    'стоп ИИ': run(variant(o, stop=s_ai)),
                    'цели ИИ (50/50)': run(variant(o, targets=t_smc_stop, fractions=fr(t_smc_stop))),
                    'сопровождение ИИ': run(variant(o, be_1r=True, ttl_h=12), be_after_tp1=True, cancel=True),
                    'срок заявки 12 ч': run(variant(o, ttl_h=12)),
                    'всё как у ИИ': run(variant(o, stop=s_ai, targets=t_ai_stop, fractions=fr(t_ai_stop),
                                                be_1r=True, ttl_h=12), be_after_tp1=True, cancel=True),
                }
                # Сравнение — только там, где исполнились все варианты с тем же входом.
                if any(v is None for v in got.values()):
                    continue
                for n, v in got.items():
                    rows[n].append(v)
        print(f'\n== {period}: сравнимых сетапов {len(rows[names[0]])}; отсеяно правилами ИИ: {skipped}', flush=True)
        for n in names:
            r = np.array(rows[n])
            totals[n] += list(r)
            print(f'   {n:22s} {r.mean():+.3f} R/сд   в плюс {np.mean(r > 0.05) * 100:3.0f}%', flush=True)
    print('\nВСЕ ПЯТЬ ПЕРИОДОВ, одни и те же сетапы SMC:')
    for n in names:
        r = np.array(totals[n])
        lo, hi = ci(r)
        print(f'   {n:22s} {len(r):5d} сд  {r.mean():+.3f} R/сд [{lo:+.3f}; {hi:+.3f}]  в плюс {np.mean(r > 0.05) * 100:3.0f}%')
    r = np.array(base_all)
    print(f'   (все сетапы SMC без отбора по правилам ИИ: {len(r)} сд, {r.mean():+.3f} R/сд)')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
