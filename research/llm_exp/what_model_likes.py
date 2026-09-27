"""
Что ценит модель и что работает на деле: ранговая связь каждого признака
карточки с оценкой модели и с исходом сделки.

Если модель ценит то же, что связано с исходом, — у неё есть навык (или она
повторяет очевидное правило, которое код применил бы точнее). Если ценит
обратное — опыт A: тесный стоп, близкая цель, спокойный рынок.

Запуск (в папке с данными опыта):
    python what_model_likes.py results_C.jsonl smc_cards.json smc_outcomes.json
"""
import json
import sys

import numpy as np
import pandas as pd


def main(results, cards_path, outcomes_path):
    probs = {}
    for line in open(results, encoding='utf-8'):
        probs.update(json.loads(line)['probs'])
    cards = pd.DataFrame(json.load(open(cards_path, encoding='utf-8'))).set_index('id')
    outs = pd.DataFrame(json.load(open(outcomes_path, encoding='utf-8'))).set_index('id')
    ids = [i for i in probs if i in cards.index and i in outs.index]
    frame = cards.loc[ids].copy()
    frame['model'] = [probs[i] for i in ids]
    frame['r'] = outs.loc[ids, 'r'].to_numpy()
    frame['long'] = (frame['side'] == 'лонг').astype(float)
    frame['last3'] = [sum(x[-3:]) for x in frame['last12']]
    frame['last12s'] = [sum(x) for x in frame['last12']]
    rows = []
    for col in frame.columns:
        if col in ('model', 'r', 'side', 'last12') or not pd.api.types.is_numeric_dtype(frame[col]):
            continue
        x = frame[col].astype(float)
        if x.notna().sum() < 30 or x.nunique() < 3 and col != 'long':
            continue
        rm = x.corr(frame['model'], method='spearman')
        rr = x.corr(frame['r'], method='spearman')
        # С исходом в R ранговая связь смешивает шанс успеха с глубиной
        # проигрыша (у тесного стопа комиссии делают −1R глубже) — поэтому
        # отдельно связь с самим успехом (цель раньше стопа).
        rw = x.corr((frame['r'] > 0.05).astype(float), method='spearman')
        rows.append((col, rm, rr, rw))
    table = pd.DataFrame(rows, columns=['признак', 'с оценкой модели', 'с исходом R', 'с успехом']).set_index('признак')
    table['согласие'] = np.sign(table['с оценкой модели']) == np.sign(table['с успехом'])
    table = table.reindex(table['с оценкой модели'].abs().sort_values(ascending=False).index)
    print(f'сетапов {len(frame)}; ранговая связь (Спирмен), по силе связи с оценкой модели:')
    print(table.round(3).to_string())
    top = table.head(8)
    print(f'\nиз 8 признаков, которые модель ценит сильнее всего, по знаку совпадают с успехом сделки: '
          f'{int(top["согласие"].sum())}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(*sys.argv[1:4])
