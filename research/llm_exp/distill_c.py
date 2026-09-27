"""
Вкус модели — в код, и проверка на сетапах, которых модель не видела.

Опыт C: модель оценила 400 анонимных сетапов SMC. Здесь гребневая регрессия
учится ПОВТОРЯТЬ её оценки по признакам карточки (не исходы!), а затем этот
«слепок вкуса» ставит оценки остальным сетапам SMC из полного набора
(ai_filter_study, 10 пар SMC, пять периодов). Если отбор слепком улучшает
исход на невиданных сетапах во всех периодах — у модели есть полезный вкус,
и держать его можно кодом. Если нет — результат C на 400 карточках случаен.

Признаки — те же, что в карточке (build_cards.card), кроме last12 (ход 12
часов нужен из свечей; в карточке он есть, в слепке — сумма за 3 и 12 ч).

Запуск:
    python research/llm_exp/distill_c.py research/results/llm_exp/results_C.jsonl
"""
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ai_filter_study as F                           # noqa: E402
import backtest_smc as bt                             # noqa: E402
from common import ci                                 # noqa: E402

DATA = os.path.join(os.path.dirname(HERE), 'results', 'llm_exp')
# поле карточки → колонка набора ai_filter_study
FIELDS = {'entry_away': 'dist_entry_pct', 'stop': 'stop_pct', 'rr': 'rr1', 'r4': 'ret_4h', 'r24': 'ret_24h',
          'r7d': 'ret_7d', 'r30d': 'ret_30d', 'pos30': 'pos30', 'atr': 'atr_pct', 'atr_rank': 'atr_rank',
          'adr': 'adr_pct', 'vol': 'vol_ratio', 'ema': 'ema50d_gap', 'er': 'er_30d', 'btc24': 'btc_ret_24h',
          'btc7': 'btc_ret_7d', 'fund': 'funding', 'fund3': 'funding_3', 'oi4': 'oi_chg_4h', 'oi24': 'oi_chg_24h',
          'hour': 'hour', 'conf': 'confluence'}


def design(frame, cols, stats=None):
    x = frame[cols].astype(float)
    if stats is None:
        stats = (x.median(), x.std().replace(0, 1.0))
    med, sd = stats
    z = ((x.fillna(med) - med) / sd).clip(-4, 4)
    return np.column_stack([np.ones(len(z)), z.to_numpy()]), stats


def auc(scores, labels):
    s = pd.Series(scores).rank()
    labels = np.asarray(labels, bool)
    n1, n0 = labels.sum(), (~labels).sum()
    return (s[labels].sum() - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else float('nan')


def main(results_path):
    probs = {}
    for line in open(results_path, encoding='utf-8'):
        probs.update(json.loads(line)['probs'])
    cards = pd.DataFrame(json.load(open(os.path.join(DATA, 'smc_cards.json'), encoding='utf-8'))).set_index('id')
    outs = pd.DataFrame(json.load(open(os.path.join(DATA, 'smc_outcomes.json'), encoding='utf-8'))).set_index('id')
    ids = [i for i in probs if i in cards.index]
    seen = cards.loc[ids].copy()
    seen['model'] = [probs[i] for i in ids]
    seen['side_sign'] = np.where(seen['side'] == 'лонг', 1.0, -1.0)
    cols = [c for c in FIELDS if c in seen.columns] + ['side_sign']
    x, stats = design(seen, cols)
    lam = 5.0
    w = np.linalg.solve(x.T @ x + lam * np.eye(x.shape[1]), x.T @ seen['model'].to_numpy(dtype=float))
    fit = x @ w
    print(f'слепок вкуса: {len(seen)} карточек, связь слепка с оценками модели '
          f'{np.corrcoef(fit, seen["model"])[0, 1]:+.2f}')
    order = np.argsort(-np.abs(w[1:]))
    print('   что слепок ценит (вес на одно стандартное отклонение признака): '
          + ', '.join(f'{cols[k]} {w[1 + k]:+.2f}' for k in order[:8]))

    data = F.load()
    pool = data[(data.skeleton == 'smc') & data.filled & data.r.notna() & data.pair.isin(bt.DEFAULT_PAIRS)].copy()
    used = {(outs.loc[i, 'pair'], int(outs.loc[i, 't'])) for i in ids}
    pool = pool[[(p, int(t)) not in used for p, t in zip(pool['pair'], pool['t'])]].copy()
    for card_name, col in FIELDS.items():
        pool[card_name] = pool[col] if col in pool else np.nan
    pool['side_sign'] = np.where(pool['side'] == 'LONG', 1.0, -1.0)
    pool['score'] = design(pool, cols, stats)[0] @ w
    pool['win'] = pool['r'] > 0.05
    print(f'\nневиданные сетапы SMC: {len(pool)}; AUC слепка {auc(pool["score"], pool["win"]):.3f}')
    for label, part in (('все', pool), ('стоп от 0.75% (брокер пропустил бы)', pool[pool['stop_pct'] >= 0.75])):
        yes = part['score'] >= part['score'].median()
        diff = part[yes]['r'].to_numpy()
        lo, hi = ci(diff)
        print(f'\n  {label}: {len(part)} сетапов, поток {part["r"].mean():+.3f}R; «да» слепка (верхняя половина) '
              f'{diff.mean():+.3f}R [{lo:+.3f}; {hi:+.3f}], «нет» {part[~yes]["r"].mean():+.3f}R')
        for period in ('bear', 'mid1', 'mid2', '12m', 'fresh'):
            k = part['period'] == period
            if k.sum() < 10:
                continue
            y = k & yes
            print(f'     {period:6s} поток {part[k]["r"].mean():+.3f}R ({k.sum()}), «да» {part[y]["r"].mean():+.3f}R '
                  f'({y.sum()}), «нет» {part[k & ~yes]["r"].mean():+.3f}R')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1])
