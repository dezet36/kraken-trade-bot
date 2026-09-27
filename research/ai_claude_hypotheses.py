"""
Гипотезы о механике рынка, записанные до замера (27.09.2026) — линия для
сравнения с тем, что найдёт модель (ai_model_research.py). Пороги — перцентили
распределения признака на train по всем парам (исхода они не знают).

Механизмы:
    поглощение     цена падала сутки, а покупки по рынку необычно высоки —
                   продавца кто-то забирает (и зеркально для шорта);
    толпа в ловушке доля счетов в лонге у дна за 30 дней, фандинг ниже нуля,
                   цена держится — шортистам больно, выжмут (и зеркально);
    разгрузка      резкий ход за 4 ч с падением ОИ — вынужденное закрытие
                   позиций, цена отходит назад;
    догон BTC      BTC сходил за 4 ч, монета стоит — догоняет;
    пробой потока  цена у края диапазона 7 дней, агрессор и ОИ за движение.

    python research/ai_claude_hypotheses.py
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_pattern_lab as L                            # noqa: E402


def train_quantiles():
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS['train'])
    frames = [feat[(feat.index >= a) & (feat.index < b)] for _, (df, feat) in L.load_all().items()]
    allf = pd.concat(frames)
    return lambda name, q: float(allf[name].quantile(q))


def rules(q):
    return [
        {'name': 'C1 поглощение, лонг', 'side': 'long', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [['ret_24h', '<', q('ret_24h', 0.10)], ['taker_24h_dev', '>', q('taker_24h_dev', 0.80)]]},
        {'name': 'C2 поглощение, шорт', 'side': 'short', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [['ret_24h', '>', q('ret_24h', 0.90)], ['taker_24h_dev', '<', q('taker_24h_dev', 0.20)]]},
        {'name': 'C3 толпа в шортах, выжимают', 'side': 'long', 'hold_hours': 48, 'stop_atr': 1.0,
         'conditions': [['buy_ratio_pct_30d', '<', 0.10], ['funding_bp', '<', 0.0], ['ret_24h', '>', -1.0]]},
        {'name': 'C4 толпа в лонгах, сливают', 'side': 'short', 'hold_hours': 48, 'stop_atr': 1.0,
         'conditions': [['buy_ratio_pct_30d', '>', 0.90], ['funding_pct_30d', '>', 0.80], ['ret_24h', '<', 1.0]]},
        {'name': 'C5 разгрузка вниз, отскок', 'side': 'long', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [['ret_4h', '<', q('ret_4h', 0.02)], ['oi_chg_4h', '<', q('oi_chg_4h', 0.05)]]},
        {'name': 'C6 разгрузка вверх, откат', 'side': 'short', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [['ret_4h', '>', q('ret_4h', 0.98)], ['oi_chg_4h', '<', q('oi_chg_4h', 0.05)]]},
        {'name': 'C7 догон BTC вверх', 'side': 'long', 'hold_hours': 12, 'stop_atr': 0.7,
         'conditions': [['btc_ret_4h', '>', q('btc_ret_4h', 0.97)], ['ret_4h', '<', q('ret_4h', 0.60)]]},
        {'name': 'C8 догон BTC вниз', 'side': 'short', 'hold_hours': 12, 'stop_atr': 0.7,
         'conditions': [['btc_ret_4h', '<', q('btc_ret_4h', 0.03)], ['ret_4h', '>', q('ret_4h', 0.40)]]},
        {'name': 'C9 пробой вверх на потоке', 'side': 'long', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [['range_pos_7d', '>', 0.97], ['taker_4h', '>', q('taker_4h', 0.90)],
                        ['oi_chg_24h', '>', q('oi_chg_24h', 0.80)]]},
        {'name': 'C10 пробой вниз на потоке', 'side': 'short', 'hold_hours': 24, 'stop_atr': 1.0,
         'conditions': [['range_pos_7d', '<', 0.03], ['taker_4h', '<', q('taker_4h', 0.10)],
                        ['oi_chg_24h', '>', q('oi_chg_24h', 0.80)]]},
    ]


def main():
    q = train_quantiles()
    for r in rules(q):
        summary, _ = L.evaluate(r, splits=('train', 'valid'))
        flag = 'ПРИНЯТО' if L.accepted(summary) else ''
        print(L.line(r['name'], summary), flag, flush=True)
        print('      ', [(c[0], c[1], round(c[2], 4)) for c in r['conditions']], flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
