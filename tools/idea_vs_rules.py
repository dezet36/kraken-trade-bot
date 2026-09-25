# -*- coding: utf-8 -*-
"""
Права ли модель по идее, и где теряется результат: её план при четырёх
способах исполнения — как у бота, лимит без условия 12 ч и 48 ч, по рынку.

«Идея» — что цена задела раньше после вердикта, цель или стоп, без входа.
Разбор и симуляция — из tools/rejected_outcomes.py. Запуск на сервере:
/opt/kraken/venv/bin/python tools/idea_vs_rules.py
"""
import collections

src = open('/opt/kraken/code/tools/rejected_outcomes.py', encoding='utf-8').read()
exec(src[:src.index('items = []')])


def prep(pair, at, p):
    df = candles(pair)
    if df is None or not len(df):
        return None
    t = np.asarray(df['timestamp'].values).astype('datetime64[ms]').astype('int64')
    hi, lo = df['high'].values.astype(float), df['low'].values.astype(float)
    cl, op = df['close'].values.astype(float), df['open'].values.astype(float)
    ts = int(datetime.datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp() * 1000)
    i0 = int(np.searchsorted(t, ts, side='right'))
    if i0 < 20 or i0 >= len(df):
        return None
    long = p['side'] == 'LONG'
    atr_pct = market_regime.volatility_pct(hi[:i0], lo[:i0], cl[:i0])
    buf = llm_decide.stop_hunt_pct(atr_pct)
    stop = p['stop_level'] * (1 - buf / 100) if long else p['stop_level'] * (1 + buf / 100)
    return dict(hi=hi, lo=lo, cl=cl, op=op, i0=i0, long=long, stop=stop, tp=p['tp'], n=len(df))


def race(d, start, entry):
    """После входа по entry с бара start: 'цель'/'стоп'/'открыта' и R за вычетом комиссий."""
    long, stop, tp = d['long'], d['stop'], d['tp']
    risk = abs(entry - stop)
    if not risk or (long and not stop < entry < tp) or (not long and not tp < entry < stop):
        return None
    cost = llm_decide.cost_in_r(entry, stop)
    for i in range(start, d['n']):
        s_hit = d['lo'][i] <= stop if long else d['hi'][i] >= stop
        t_hit = d['hi'][i] >= tp if long else d['lo'][i] <= tp
        if s_hit:
            return 'стоп', -1.0 - cost
        if t_hit:
            return 'цель', abs(tp - entry) / risk - cost
    last = d['cl'][-1]
    return 'открыта', ((last - entry) if long else (entry - last)) / risk - cost


def idea(d):
    """Что цена задела раньше после вердикта — цель или стоп, без всякого входа."""
    for i in range(d['i0'], d['n']):
        s_hit = d['lo'][i] <= d['stop'] if d['long'] else d['hi'][i] >= d['stop']
        t_hit = d['hi'][i] >= d['tp'] if d['long'] else d['lo'][i] <= d['tp']
        if s_hit:
            return 'стоп раньше'
        if t_hit:
            return 'цель раньше'
    return 'не решилось'


def limit(d, entry, ttl):
    for i in range(d['i0'], min(d['i0'] + ttl, d['n'])):
        if (d['long'] and d['lo'][i] <= entry) or (not d['long'] and d['hi'][i] >= entry):
            return race(d, i, entry)
    return ('не налился', 0.0) if d['i0'] + ttl <= d['n'] else None


def market(d):
    return race(d, d['i0'], d['op'][d['i0']])


items = []
for r in rows:
    if r['decision'] == 'enter':
        p, gate = plan_of(r.get('raw'), r.get('levels')), 'ПРИНЯТ'
    else:
        p, gate = plan_of(r.get('raw'), r.get('levels')), r['gate']
        if p is None and r.get('rejected_raw'):
            p = plan_of(r.get('rejected_raw'), r.get('levels'))
            gate = (r.get('revised_from') or r['gate']).split(':', 1)[0]
    if p is None:
        continue
    d = prep(r['pair'], r['at'], p)
    if d is None:
        continue
    items.append({'at': r['at'], 'pair': r['pair'], 'gate': gate, 'side': p['side'],
                  'idea': idea(d), 'бот': simulate(r['pair'], r['at'], p),
                  'лимит 12ч': limit(d, p['entry'], 12), 'лимит 48ч': limit(d, p['entry'], 48),
                  'по рынку': market(d)})

POL = ('бот', 'лимит 12ч', 'лимит 48ч', 'по рынку')


def show(title, sel):
    c = collections.Counter(x['idea'] for x in sel)
    decided = c['цель раньше'] + c['стоп раньше']
    share = c['цель раньше'] / decided * 100 if decided else 0
    print(f'\n{title}: {len(sel)} планов | ИДЕЯ: цель раньше стопа {c["цель раньше"]}, '
          f'стоп раньше {c["стоп раньше"]}, не решилось {c["не решилось"]} '
          f'-> права в {share:.0f}% решившихся')
    for pol in POL:
        v = [x[pol] for x in sel if x[pol] is not None]
        k = collections.Counter(o for o, _ in v)
        traded = k['цель'] + k['стоп'] + k['открыта']
        tot = sum(rr for _, rr in v)
        print(f'   {pol:10} сделок {traded:3}: цель {k["цель"]:3}, стоп {k["стоп"]:3}, открыто {k["открыта"]:2}'
              f'  | {tot:+7.2f} R  {tot * R_USD:+7.0f} $')


acc = [x for x in items if x['gate'] == 'ПРИНЯТ']
rej = [x for x in items if x['gate'] != 'ПРИНЯТ']
show('ОТКЛОНЕНЫ кодом', rej)
show('ПРИНЯТЫ кодом', acc)
show('ВСЕ планы модели', items)
for side in ('LONG', 'SHORT'):
    show(f'все, {side}', [x for x in items if x['side'] == side])

print('\nОТКЛОНЁННЫЕ, где идея была права, а бот не вошёл бы:')
miss = [x for x in rej if x['idea'] == 'цель раньше' and x['бот'] and x['бот'][0] not in ('цель', 'стоп', 'открыта')]
print('  ', len(miss), collections.Counter(x['бот'][0] for x in miss).most_common())
print('\nИДЕЯ ПО ВОРОТАМ (отклонённые): цель раньше / стоп раньше')
by = collections.defaultdict(collections.Counter)
for x in rej:
    by[x['gate']][x['idea']] += 1
for g, c in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
    print(f'   {g:30} {c["цель раньше"]:3} / {c["стоп раньше"]:3}   (не решилось {c["не решилось"]})')
