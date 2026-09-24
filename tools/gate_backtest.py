# -*- coding: utf-8 -*-
"""
Что новые ворота «план против старшего тренда» сделали бы с историей.

Берём ВСЕ планы модели (и пропущенные кодом, и отклонённые), считаем
структуру 4ч на момент разбора теми же `smc.structure`, и смотрим, какие
из них ворота бы остановили и сколько R это меняет.
"""
import sys, os, csv, json, re, datetime, collections

sys.path.insert(0, '/opt/kraken/code/Live_Bot')
os.chdir('/opt/kraken/code/Live_Bot')

import numpy as np
import exchange
from smc import structure as S

src = open('/tmp/target_pick.py', encoding='utf-8').read()
g = {'json': json, 're': re, 'np': np, 'ENTRY_TTL_H': 12, 'WATCH_H': 168}
exec(src[src.index('def parse('):src.index('def pick(')], g)
exec(src[src.index('def run('):src.index('def main(')], g)
parse, run = g['parse'], g['run']

rows = list(csv.DictReader(open('/opt/kraken/bot_data/llm_calls.csv', encoding='utf-8')))
h1, h4, st4 = {}, {}, {}


def frames(pair):
    if pair not in h1:
        try:
            h1[pair] = exchange.fetch_ohlcv('1h', limit=500, symbol=pair)
            h4[pair] = exchange.fetch_ohlcv('4h', limit=400, symbol=pair)
            st4[pair] = S.build_structure(h4[pair], tier='swing')
        except Exception:
            h1[pair] = h4[pair] = st4[pair] = None
    return h1[pair], h4[pair], st4[pair]


def htf_trend(pair, ts):
    d1, d4, st = frames(pair)
    if d4 is None or st is None:
        return None
    t4 = np.asarray(d4['timestamp'].values).astype('datetime64[ms]').astype('int64')
    j = int(np.searchsorted(t4, ts, side='right')) - 1
    if j < 10:
        return None
    return S.state_at(st, j).get('trend')


kept, blocked = [], []
for r in rows:
    if r['decision'] == 'skip' and (not r['gate'] or r['gate'] == 'модель пропустила'):
        continue
    p = parse(r)
    if not p:
        continue
    side, e, s, tp, _ = p
    d1, _, _ = frames(r['pair'])
    if d1 is None:
        continue
    ts = int(datetime.datetime.fromisoformat(
        r['at'].replace('Z', '+00:00')).timestamp() * 1000)
    out = run(d1, ts, side, e, s, tp)
    if out is None:
        continue
    trend = htf_trend(r['pair'], ts)
    against = (trend == 'BULLISH' and side == 'SHORT') or \
              (trend == 'BEARISH' and side == 'LONG')
    (blocked if against else kept).append((out, r['decision'], side))


def show(name, v):
    won = sum(1 for x, _, _ in v if x > 0)
    traded = [x for x, _, _ in v if x != 0]
    sides = collections.Counter(sd for _, _, sd in v)
    print('  %-34s %3d планов  %+7.2f R   дожили %2d, к цели %d   (LONG %d / SHORT %d)'
          % (name, len(v), sum(x for x, _, _ in v), len(traded), won,
             sides.get('LONG', 0), sides.get('SHORT', 0)))


print()
print('НОВЫЕ ВОРОТА НА ВСЕЙ ИСТОРИИ ПЛАНОВ')
print()
show('остановлены воротами', blocked)
show('прошли бы дальше', kept)
print()
passed_now = [x for x in kept if x[1] == 'enter']
blocked_now = [x for x in blocked if x[1] == 'enter']
print('  из тех, что код пропускает СЕЙЧАС (%d планов):' % (len(passed_now) + len(blocked_now)))
print('     новые ворота остановили бы %d на %+.2f R'
      % (len(blocked_now), sum(x for x, _, _ in blocked_now)))
print('     осталось бы %d на %+.2f R'
      % (len(passed_now), sum(x for x, _, _ in passed_now)))
