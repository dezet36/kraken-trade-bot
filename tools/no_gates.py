# -*- coding: utf-8 -*-
"""
Что было бы, если отдать решение модели целиком — без ворот кода.

Этот опыт уже поставлен, просто не нарочно: каждый ОТКЛОНЁННЫЙ план — это
запись того, что модель сделала бы, если её не остановить. Прогоняем обе
группы одной и той же симуляцией и складываем.
"""
import sys, os, csv, json, re, datetime, collections

sys.path.insert(0, '/opt/kraken/code/Live_Bot')
os.chdir('/opt/kraken/code/Live_Bot')

import numpy as np
import exchange

src = open('/tmp/target_pick.py', encoding='utf-8').read()
g = {'json': json, 're': re, 'np': np, 'ENTRY_TTL_H': 12, 'WATCH_H': 168}
exec(src[src.index('def parse('):src.index('def pick(')], g)
exec(src[src.index('def run('):src.index('def main(')], g)
parse, run = g['parse'], g['run']

rows = list(csv.DictReader(open('/opt/kraken/bot_data/llm_calls.csv', encoding='utf-8')))
frames = {}


def sim(r):
    p = parse(r)
    if not p:
        return None
    side, e, s, tp, _ = p
    pair = r['pair']
    if pair not in frames:
        try:
            frames[pair] = exchange.fetch_ohlcv('1h', limit=500, symbol=pair)
        except Exception:
            frames[pair] = None
    if frames[pair] is None:
        return None
    ts = int(datetime.datetime.fromisoformat(
        r['at'].replace('Z', '+00:00')).timestamp() * 1000)
    return run(frames[pair], ts, side, e, s, tp)


passed = [r for r in rows if r['decision'] == 'enter']
blocked = [r for r in rows if r['decision'] == 'skip' and r['gate']
           and r['gate'] != 'модель пропустила']
acc = [x for x in (sim(r) for r in passed) if x is not None]
ref = [x for x in (sim(r) for r in blocked) if x is not None]


def line(name, v):
    traded = [x for x in v if x != 0]
    won = sum(1 for x in traded if x > 0)
    return '%-32s %3d шт  %+7.2f R   до сделки дожили %2d, из них к цели %d' % (
        name, len(v), sum(v), len(traded), won)


print()
print('ЧТО БЫЛО БЫ БЕЗ ВОРОТ (одна симуляция для обеих групп)')
print()
print(' ', line('планы, которые код ПРОПУСТИЛ', acc))
print(' ', line('планы, которые код ОТКЛОНИЛ', ref))
print('  ' + '-' * 78)
print(' ', line('ВСЁ, что модель предложила', acc + ref))
print()
print('  ворота сберегли примерно %.0f R' % (-sum(ref)))
print()
print('ПО ВОРОТАМ — что именно они остановили:')
by = collections.defaultdict(list)
for r in blocked:
    v = sim(r)
    if v is not None:
        by[r['gate']].append(v)
for k in sorted(by, key=lambda k: sum(by[k])):
    v = by[k]
    traded = [x for x in v if x != 0]
    print('  %-30s %3d планов  %+7.2f R  (дожили %d, к цели %d)'
          % (k, len(v), sum(v), len(traded), sum(1 for x in traded if x > 0)))
