"""
Анонимные карточки сетапов для опытов «модель оценивает сетап».

Карточка — признаки сетапа на момент постановки без монеты и даты (цены — в
процентах от цены, ход — в сторону сделки): так модель не может «вспомнить»
историю рынка. Исходы и статистическая база (гребневая регрессия, обученная
на ДРУГИХ периодах) лежат отдельно и на сервер не уходят.

    fibo — 600 сделок Фибо периода 12m (опыты A и B, 27.09.2026);
    smc  — 400 сделок SMC на её 10 парах, все периоды (опыт C).

Запуск:
    python research/llm_exp/build_cards.py fibo <папка>
    python research/llm_exp/build_cards.py smc <папка>
Дальше — README.md рядом.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ai_doctrine as D                               # noqa: E402
import ai_filter_study as F                           # noqa: E402
import backtest_smc as bt                             # noqa: E402


def last12(period, pair, t_ms, cache={}):
    key = (period, pair)
    if key not in cache:
        cache[key] = D.load(D.PERIODS[period], pair, '1h')
    a = cache[key]
    i = int(np.searchsorted(a[:, 0], t_ms - 3_600_000, side='right')) - 1
    c = a[i - 12:i + 1, 4]
    return [round((c[k + 1] / c[k] - 1) * 100, 2) for k in range(12)]


def card(prefix, n, row):
    sgn = 1 if row.side == 'LONG' else -1
    out = {'id': f'{prefix}{n:03d}', 'side': 'лонг' if row.side == 'LONG' else 'шорт',
           'entry_away': round(row.dist_entry_pct, 2), 'stop': round(row.stop_pct, 2), 'rr': round(row.rr1, 2),
           'r4': round(row.ret_4h, 2), 'r24': round(row.ret_24h, 2), 'r7d': round(row.ret_7d, 1),
           'r30d': round(row.ret_30d, 1), 'pos30': round(row.pos30), 'atr': round(row.atr_pct, 2),
           'atr_rank': round(row.atr_rank), 'adr': round(row.adr_pct, 1), 'vol': round(row.vol_ratio, 2),
           'ema': round(row.ema50d_gap, 1), 'er': round(row.er_30d, 2), 'btc24': round(row.btc_ret_24h, 2),
           'btc7': round(row.btc_ret_7d, 1),
           'fund': None if pd.isna(row.funding) else round(row.funding, 2),
           'fund3': None if pd.isna(row.funding_3) else round(row.funding_3, 2),
           'oi4': None if pd.isna(row.oi_chg_4h) else round(row.oi_chg_4h, 2),
           'oi24': None if pd.isna(row.oi_chg_24h) else round(row.oi_chg_24h, 2),
           'hour': int(row.hour), 'last12': [x * sgn for x in last12(row.period, row.pair, int(row.t))]}
    if 'confluence' in row and not pd.isna(row.confluence):
        out['conf'] = float(row.confluence)
    return out


def main(kind, folder):
    os.makedirs(folder, exist_ok=True)
    data = F.load()
    cols = [c for c in F.NUMERIC if c in data and data[c].notna().mean() > 0.5 and c != 'fomc_hours']
    if kind == 'fibo':
        pool = data[(data.skeleton == 'fibo') & data.filled & data.r.notna()].copy()
        test = pool[pool.period == '12m'].copy()
        train = pool[pool.fold.isin(['bear', 'mid1', 'mid2'])]
        xt, st = F.design(train, cols)
        test['ml'] = F.design(test, cols, st)[0] @ F.fit_ridge(xt, np.clip(train.r.to_numpy(), -2, 6))
        rng, n, prefix = np.random.default_rng(20260927), 600, 'S'
    else:
        pool = data[(data.skeleton == 'smc') & data.filled & data.r.notna() & data.pair.isin(bt.DEFAULT_PAIRS)].copy()
        pool['ml'] = np.nan
        for fold in F.FOLDS:
            tr, te = pool[pool.fold != fold], pool[pool.fold == fold]
            xt, st = F.design(tr, cols)
            pool.loc[te.index, 'ml'] = F.design(te, cols, st)[0] @ F.fit_ridge(xt, np.clip(tr.r.to_numpy(), -2, 6))
        test, rng, n, prefix = pool, np.random.default_rng(20260928), 400, 'C'
    sample = test.iloc[rng.permutation(len(test))[:n]].reset_index(drop=True)
    cards = [card(prefix, i, row) for i, row in sample.iterrows()]
    outcomes = [{'id': c['id'], 'r': float(row.r), 'ml': float(row.ml), 'pair': row.pair, 't': int(row.t),
                 'side': row.side, 'period': row.period} for c, (_, row) in zip(cards, sample.iterrows())]
    json.dump(cards, open(os.path.join(folder, 'cards.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    json.dump(outcomes, open(os.path.join(folder, 'outcomes.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'{kind}: {len(cards)} карточек в {folder}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1], sys.argv[2])
