"""
Поток вживую (лента сделок, ликвидации, ОИ, базис) против хода цены вперёд.

Данные бота с сервера (bot_data/positioning/*.jsonl): delta.jsonl
(поминутная дельта агрессора), liquidations.jsonl, open_interest.jsonl,
premium.jsonl, long_short.jsonl; с 27.09.2026 ещё book.jsonl (снимки
стакана) — его признаки сюда добавить, когда накопится хотя бы месяц.
Свечи 5m — любая папка с <PAIR>_5m.pkl (research/ai_live_plans.py качает
такие в <папка>/candles).

Первый прогон 26.09.2026 (8 суток, 3547 пар-часов): лента и ликвидации
направления не дали; премия — IC −0.17/−0.09 на 4 ч в двух половинах недели,
но на истории (research/ai_premium_study.py) не подтвердилась.

Запуск:
    python research/ai_flow_live_study.py <папка positioning> <папка со свечами 5m>

На каждом закрытии часа по каждой паре: признаки за прошлый час/4 часа и ход
цены на 1 и 4 часа вперёд. Сила связи — ранговая корреляция (IC); устойчивость —
отдельно в первой и второй половине периода; всё вместе — гребневая регрессия,
обученная на одной половине и проверенная на другой.
"""
import bisect
import json
import os
import pickle
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')
HERE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sim-data', 'positioning')
CANDLES = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sim-data', 'ai', 'candles')
H, M = 3_600_000, 60_000


def load_jsonl(name):
    return [json.loads(line) for line in open(os.path.join(HERE, name), encoding='utf-8')]


def main():
    delta = defaultdict(dict)                      # pair -> minute ts -> (buy, sell)
    for r in load_jsonl('delta.jsonl'):
        delta[r['pair']][int(r['ts'])] = (float(r.get('buy') or 0), float(r.get('sell') or 0))
    liq = defaultdict(list)                        # pair -> [(ts, side, notional)]
    for r in load_jsonl('liquidations.jsonl'):
        liq[r['pair']].append((int(r['ts']), r['side'], float(r['price']) * float(r['size'])))
    hourly = {}
    for name in ('open_interest', 'premium', 'long_short'):
        d = defaultdict(dict)
        for r in load_jsonl(f'{name}.jsonl'):
            d[r['pair']][int(r['ts'])] = float(r['value']) if r.get('value') is not None else np.nan
        hourly[name] = d

    rows = []
    for pair in sorted(delta):
        path = os.path.join(CANDLES, f'{pair}_5m.pkl')
        if not os.path.exists(path):
            continue
        c5 = pickle.load(open(path, 'rb'))
        ts5 = [c[0] for c in c5]
        close_at = lambda t: c5[bisect.bisect_right(ts5, t - 300_000) - 1][4] if bisect.bisect_right(ts5, t - 300_000) > 0 else None  # noqa: E731
        minutes = delta[pair]
        mts = np.array(sorted(minutes))
        buys = np.array([minutes[t][0] for t in mts])
        sells = np.array([minutes[t][1] for t in mts])
        cum_b, cum_s = np.r_[0, np.cumsum(buys)], np.r_[0, np.cumsum(sells)]
        lq = sorted(liq.get(pair, []))
        lq_ts = [x[0] for x in lq]
        first, last = mts[0] + 4 * H, mts[-1] - 4 * H
        t = (first // H + 1) * H
        while t <= last:
            def share(span):
                a, b = np.searchsorted(mts, t - span), np.searchsorted(mts, t)
                tot = (cum_b[b] - cum_b[a]) + (cum_s[b] - cum_s[a])
                cover = (b - a) / (span / M)
                return ((cum_b[b] - cum_b[a]) - (cum_s[b] - cum_s[a])) / tot if tot > 0 and cover > 0.5 else np.nan
            a, b = bisect.bisect_left(lq_ts, t - H), bisect.bisect_left(lq_ts, t)
            long_l = sum(x[2] for x in lq[a:b] if x[1] == 'long')
            short_l = sum(x[2] for x in lq[a:b] if x[1] == 'short')
            p0, p1, p4 = close_at(t), close_at(t + H), close_at(t + 4 * H)
            pm1, pm4 = close_at(t - H), close_at(t - 4 * H)
            if None in (p0, p1, p4, pm1, pm4):
                t += H
                continue
            oi = hourly['open_interest'][pair]
            oi_now, oi_4 = oi.get(t - H), oi.get(t - 5 * H)
            prem = hourly['premium'][pair].get(t - H, np.nan)
            ls_now, ls_4 = hourly['long_short'][pair].get(t - H), hourly['long_short'][pair].get(t - 5 * H)
            rows.append({
                'pair': pair, 't': t,
                'delta_1h': share(H), 'delta_4h': share(4 * H),
                'liq_imb_1h': (short_l - long_l) / (short_l + long_l) if short_l + long_l > 0 else 0.0,
                'liq_size_1h': np.log1p(short_l + long_l),
                'ret_1h': (p0 / pm1 - 1) * 100, 'ret_4h': (p0 / pm4 - 1) * 100,
                'oi_chg_4h': (oi_now / oi_4 - 1) * 100 if oi_now and oi_4 else np.nan,
                'premium': prem * 1e4 if prem == prem else np.nan,
                'ls_chg_4h': (ls_now / ls_4 - 1) * 100 if ls_now and ls_4 else np.nan,
                'fwd_1h': (p1 / p0 - 1) * 100, 'fwd_4h': (p4 / p0 - 1) * 100,
            })
            t += H
    f = pd.DataFrame(rows)
    mid = f.t.quantile(0.5)
    f['half'] = np.where(f.t < mid, 'первая', 'вторая')
    print(f'точек {len(f)}, пар {f.pair.nunique()}, часов {f.t.nunique()}')
    feats = ['delta_1h', 'delta_4h', 'liq_imb_1h', 'liq_size_1h', 'ret_1h', 'ret_4h', 'oi_chg_4h', 'premium', 'ls_chg_4h']
    print(f'\n{"признак":12s} {"IC→1ч":>7s} {"1-я пол.":>8s} {"2-я пол.":>8s} {"IC→4ч":>7s} {"1-я пол.":>8s} {"2-я пол.":>8s}')
    for k in feats:
        cells = []
        for tgt in ('fwd_1h', 'fwd_4h'):
            for part in (f, f[f.half == 'первая'], f[f.half == 'вторая']):
                m = part[k].notna()
                cells.append(np.corrcoef(part.loc[m, k].rank(), part.loc[m, tgt].rank())[0, 1] if m.sum() > 50 else np.nan)
        print(f'{k:12s} ' + ' '.join(f'{c:+8.3f}' for c in cells))
    # Гребневая: учимся на одной половине, проверяем на другой — отдельно для 1ч и 4ч.
    for tgt, cost in (('fwd_1h', 0.11), ('fwd_4h', 0.11)):
        out = []
        for train_half, test_half in (('первая', 'вторая'), ('вторая', 'первая')):
            tr, te = f[f.half == train_half], f[f.half == test_half]
            med = tr[feats].median()
            xt = tr[feats].fillna(med).to_numpy(); xs = te[feats].fillna(med).to_numpy()
            mu, sd = xt.mean(0), xt.std(0) + 1e-9
            xt, xs = (xt - mu) / sd, (xs - mu) / sd
            xt, xs = np.c_[np.ones(len(xt)), xt], np.c_[np.ones(len(xs)), xs]
            y = tr[tgt].clip(tr[tgt].quantile(0.01), tr[tgt].quantile(0.99)).to_numpy()
            w = np.linalg.solve(xt.T @ xt + np.diag([0] + [50] * len(feats)), xt.T @ y)
            pred = xs @ w
            yt = te[tgt].to_numpy()
            ic = np.corrcoef(pd.Series(pred).rank(), pd.Series(yt).rank())[0, 1]
            signed = np.sign(pred) * yt
            strong = np.abs(pred) >= np.quantile(np.abs(pred), 0.8)
            out.append((ic, signed.mean(), signed[strong].mean(), np.mean(np.sign(pred) == np.sign(yt))))
        print(f'\nвместе, {tgt}: IC вне выборки {out[0][0]:+.3f} / {out[1][0]:+.3f}; ход в сторону прогноза '
              f'{out[0][1]:+.3f}% / {out[1][1]:+.3f}% (самые сильные 20%: {out[0][2]:+.3f}% / {out[1][2]:+.3f}%); '
              f'знак угадан {out[0][3] * 100:.1f}% / {out[1][3] * 100:.1f}%; круг издержек {cost}%')


if __name__ == '__main__':
    main()
