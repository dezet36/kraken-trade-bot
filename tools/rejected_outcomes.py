# -*- coding: utf-8 -*-
"""
Что было бы с планами ИИ, которые отклонил код, — и с принятыми, той же меркой.

Исполнение повторяет бота: «рынок обогнал план» по трём свечам к вердикту;
условие входа (strategy_llm.condition_met) по закрытым часам до 12 ч, цель
раньше условия — план снят; лимит на вход 12 ч; стоп = уровень ± буфер
(llm_decide.stop_hunt_pct от ATR 1ч); выход — первая цель или стоп (в одном
часе стоп считается раньше цели); комиссии — llm_decide.cost_in_r. Не
дошедшие ни до цели, ни до стопа — по последней цене, помечены «открыта».

Симулятор оптимистичен: на принятых планах 19–23.09 он даёт +6.9 R, в жизни
вышло −9.5 R (17 сделок). Направление вывода по отклонённым от этого только
крепче. Запуск на сервере: /opt/kraken/venv/bin/python tools/rejected_outcomes.py
"""
import sys, os, csv, json, re, datetime, collections

os.environ.setdefault('BOT_DATA_DIR', '/opt/kraken/bot_data')
sys.path.insert(0, '/opt/kraken/code/Live_Bot')
os.chdir('/opt/kraken/code/Live_Bot')

import numpy as np
import config
import exchange
import llm_decide
import market_regime
import strategy_llm

TTL_H = int(getattr(config, 'LLM_TRIGGER_TTL_H', 12) or 12)
H = 3_600_000
R_USD = 50.0            # 1R = 0.5% депозита ИИ $10 000
SPLIT = '2026-09-23T12'  # начало периода «ноль планов»

rows = list(csv.DictReader(open('/opt/kraken/bot_data/llm_calls.csv', encoding='utf-8')))


def plan_of(text, levels_json):
    if not text:
        return None
    if '</think>' in text:
        text = text.split('</think>', 1)[1]
    try:
        d = json.loads(text.strip())
    except Exception:
        return None
    if (d.get('d') or '') != 'enter':
        return None
    try:
        by_id = {a: float(b) for a, b, _k in json.loads(levels_json or '[]')}
    except Exception:
        return None

    def price(v):
        if v is None:
            return None
        m = re.match(r'\s*(L\d+)', str(v))
        if m and m.group(1) in by_id:
            return by_id[m.group(1)]
        m = re.search(r'\(([0-9.eE+-]+)\)', str(v))
        try:
            return float(m.group(1)) if m else None
        except Exception:
            return None

    side = (d.get('side') or '').upper()
    e, s = price(d.get('entry')), price(d.get('stop'))
    tps = [t for t in (price(x) for x in (d.get('tp') or [])) if t]
    trig = d.get('trigger') if isinstance(d.get('trigger'), dict) else {}
    when = trig.get('when') or 'now'
    if side not in ('LONG', 'SHORT') or not e or not s or not tps:
        return None
    if (side == 'LONG' and not s < e < tps[0]) or (side == 'SHORT' and not s > e > tps[0]):
        return None
    return {'side': side, 'entry': e, 'stop_level': s, 'tp': tps[0],
            'when': when, 'tlevel': price(trig.get('level'))}


frames = {}


def candles(pair):
    if pair not in frames:
        try:
            frames[pair] = exchange.fetch_ohlcv('1h', limit=1000, symbol=pair)
        except Exception:
            frames[pair] = None
    return frames[pair]


def simulate(pair, at, p):
    """-> (исход, R) — исход: обогнал / без условия / цель без входа / не налился / цель / стоп / открыта."""
    df = candles(pair)
    if df is None or not len(df):
        return None
    t = np.asarray(df['timestamp'].values).astype('datetime64[ms]').astype('int64')
    hi, lo = df['high'].values.astype(float), df['low'].values.astype(float)
    cl, vol = df['close'].values.astype(float), df['volume'].values.astype(float)
    ts = int(datetime.datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp() * 1000)
    i0 = int(np.searchsorted(t, ts, side='right'))
    if i0 < 20 or i0 >= len(df):
        return None
    side, long = p['side'], p['side'] == 'LONG'
    entry, tp = p['entry'], p['tp']
    atr_pct = market_regime.volatility_pct(hi[:i0], lo[:i0], cl[:i0])
    buf = llm_decide.stop_hunt_pct(atr_pct)
    stop = p['stop_level'] * (1 - buf / 100) if long else p['stop_level'] * (1 + buf / 100)
    risk = abs(entry - stop)
    cost = llm_decide.cost_in_r(entry, stop)

    k = i0 - 1                                   # свеча, в которой пришёл вердикт
    rh, rl = hi[max(0, k - 2):k + 1].max(), lo[max(0, k - 2):k + 1].min()
    if (long and (rh >= tp or rl <= stop)) or (not long and (rl <= tp or rh >= stop)):
        return 'обогнал', 0.0

    start = i0
    if p['when'] != 'now' and p['tlevel']:
        med = float(np.median(vol[max(0, i0 - 49):i0 - 1])) if i0 > 2 else 0.0
        met_at = None
        for i in range(i0, len(df)):
            if (t[i] - ts) >= TTL_H * H:
                break
            if (long and hi[i] >= tp) or (not long and lo[i] <= tp):
                return 'цель без входа', 0.0
            bars = [(int(t[j]) + H, 0, hi[j], lo[j], cl[j], vol[j]) for j in range(i0, i + 1)]
            if strategy_llm.condition_met(p['when'], p['tlevel'], side, bars, med):
                met_at = i
                break
        if met_at is None:
            return 'без условия', 0.0
        start = met_at + 1

    filled = None
    for i in range(start, min(start + TTL_H, len(df))):
        if (long and lo[i] <= entry) or (not long and hi[i] >= entry):
            filled = i
            break
    if filled is None:
        return ('не налился', 0.0) if start + TTL_H <= len(df) else None

    for i in range(filled, len(df)):
        s_hit = lo[i] <= stop if long else hi[i] >= stop
        t_hit = hi[i] >= tp if long else lo[i] <= tp
        if s_hit:
            return 'стоп', -1.0 - cost
        if t_hit:
            return 'цель', abs(tp - entry) / risk - cost
    last = cl[-1]
    return 'открыта', ((last - entry) if long else (entry - last)) / risk - cost


def gate_of(r):
    if r['decision'] == 'enter':
        return 'ПРИНЯТ'
    return r['gate']


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
    out = simulate(r['pair'], r['at'], p)
    if out is None:
        continue
    items.append({'at': r['at'], 'pair': r['pair'], 'gate': gate, 'side': p['side'],
                  'when': p['when'], 'outcome': out[0], 'r': out[1]})

ORDER = ('цель', 'стоп', 'открыта', 'не налился', 'без условия', 'цель без входа', 'обогнал')


def block(title, sel):
    c = collections.Counter(x['outcome'] for x in sel)
    total = sum(x['r'] for x in sel)
    entered = c['цель'] + c['стоп'] + c['открыта']
    print(f'{title:34} {len(sel):3} | вошли {entered:3}: цель {c["цель"]:2}, стоп {c["стоп"]:2}, '
          f'открыто {c["открыта"]:2} | не вошли {len(sel) - entered:3} | '
          f'{total:+7.2f} R  {total * R_USD:+8.0f} $')
    if not title.startswith(' '):
        print(' ' * 36 + 'не вошли: ' + ', '.join(f'{k} {c[k]}' for k in ORDER[3:] if c[k]))


print('Симуляция по часовым свечам, R — за вычетом комиссий, 1R = $%.0f' % R_USD)
print('Журнал с %s по %s, планов «войти» с разобранным ответом: %d'
      % (rows[0]['at'][:16], rows[-1]['at'][:16], len(items)))
for period, sel in (('ВСЁ ВРЕМЯ', items),
                    ('ДО 23.09 12:00', [x for x in items if x['at'] < SPLIT]),
                    ('С 23.09 12:00 (ноль планов)', [x for x in items if x['at'] >= SPLIT])):
    print()
    print('=' * 20, period)
    acc = [x for x in sel if x['gate'] == 'ПРИНЯТ']
    rej = [x for x in sel if x['gate'] != 'ПРИНЯТ']
    block('ПРИНЯТЫ кодом', acc)
    block('ОТКЛОНЕНЫ кодом', rej)
    by = collections.defaultdict(list)
    for x in rej:
        by[x['gate']].append(x)
    for g in sorted(by, key=lambda g: sum(x['r'] for x in by[g])):
        block('  ' + g, by[g])

print()
print('ОТКЛОНЁННЫЕ, дошедшие до сделки (с 23.09 12:00):')
for x in items:
    if x['at'] >= SPLIT and x['gate'] != 'ПРИНЯТ' and x['outcome'] in ('цель', 'стоп', 'открыта'):
        print(f"  {x['at'][5:16]} {x['pair']:10} {x['side']:5} {x['when']:24} {x['gate']:28} "
              f"{x['outcome']:8} {x['r']:+.2f} R")
