"""
Оценка ИИ-контроля торгового счёта (Live_Bot/accounts/ai_control.py, с 10.10.2026).

Контроль — тестовый счёт той же стратегии: он берёт те же сетапы без ИИ и
ведёт их по правилам. Отсюда три вопроса:
1. Допуск: сделки, которые ИИ ПРОПУСТИЛ, — сколько они дали на тестовом счёте
   (paper_trades.jsonl), против тех, что ИИ взял.
2. Действия: на каждой сделке, где ИИ перенёс стоп, закрыл половину или всё, —
   итог сделки на торговом счёте против той же сделки на тестовом (та же
   стратегия, пара, сторона, открытие в пределах часа).
3. Итог: R на сделку торгового счёта и тестового на одних и тех же сделках.

Пользы нет, пока «пропущенные» не хуже «взятых» и действия в сумме не дают R
больше нуля — так и записывается. Судить после ≥ 50 решений допуска и ≥ 30
действий.

    python research/ai_control_eval.py <каталог данных бота>
"""
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np


def rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as fh:
        return [json.loads(x) for x in fh if x.strip()]


def ts(text):
    return datetime.fromisoformat(str(text).replace('Z', '+00:00'))


def twin(paper, strategy, pair, direction, after, within):
    """Сделка тестового счёта: та же стратегия/пара/сторона, открыта в окне после after."""
    best = None
    for t in paper:
        if t.get('strategy') != strategy or t.get('pair') != pair:
            continue
        if direction and str(t.get('direction', '')).upper() != direction:
            continue
        opened = ts(t['open_time'])
        if after - timedelta(hours=1) <= opened <= after + within:
            if best is None or opened < ts(best['open_time']):
                best = t
    return best


def mean(x):
    return f'{np.mean(x):+.3f}' if len(x) else '—'


def main(data):
    log = rows(os.path.join(data, 'ai_control.jsonl'))
    paper = [t for t in rows(os.path.join(data, 'paper_trades.jsonl')) if t.get('pnl_r') not in (None, '')]
    acct = rows(os.path.join(data, 'trading_trades.jsonl'))

    gates = [r for r in log if r.get('kind') == 'gate' and 'take' in r]
    took, skipped, unknown = [], [], 0
    for g in gates:
        t = twin(paper, g.get('strategy'), g.get('pair'), None, ts(g['at']), timedelta(days=3))
        if t is None:
            unknown += 1
            continue
        (took if g['take'] else skipped).append(float(t['pnl_r']))
    print(f'Допуск: решений {len(gates)}, нет ответа {sum(1 for r in log if r.get("kind") == "gate_timeout")}, '
          f'без пары на тесте {unknown}')
    print(f'   ИИ взял бы:    {len(took):3d} сд, на тестовом счёте {mean(took)} R/сд')
    print(f'   ИИ пропустил:  {len(skipped):3d} сд, на тестовом счёте {mean(skipped)} R/сд')

    acts = [r for r in log if r.get('kind') == 'action' and r.get('applied')]
    deltas = []
    by_kind = {}
    for a in acts:
        own = next((t for t in acct if t.get('account') == a['account'] and t.get('trade_id') == a['trade_id']), None)
        if own is None:
            continue
        t = twin(paper, own['strategy'], own['pair'], own['direction'], ts(own['opened_at']), timedelta(hours=1))
        if t is None:
            continue
        d = float(own['pnl_r']) - float(t['pnl_r'])
        deltas.append(d)
        by_kind.setdefault(a['applied'], []).append(d)
    print(f'\nДействия: применено {len(acts)}, отказано ограничителями '
          f'{sum(1 for r in log if r.get("kind") == "action" and r.get("refused"))}')
    print(f'   разница «торговый счёт − тестовый» на тех же сделках: {len(deltas)} сд, {mean(deltas)} R/сд, '
          f'сумма {np.sum(deltas) if deltas else 0:+.2f}R')
    for k, v in by_kind.items():
        print(f'   {k:6s} {len(v):3d}: {mean(v)} R/сд')

    pairs = []
    for own in acct:
        t = twin(paper, own.get('strategy'), own.get('pair'), own.get('direction'),
                 ts(own['opened_at']), timedelta(hours=1))
        if t is not None:
            pairs.append((float(own['pnl_r']), float(t['pnl_r'])))
    if pairs:
        a, b = zip(*pairs)
        print(f'\nИтог на общих сделках ({len(pairs)}): торговый счёт {mean(a)} R/сд, тестовый {mean(b)} R/сд')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '.')
