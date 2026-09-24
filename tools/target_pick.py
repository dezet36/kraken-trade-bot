# -*- coding: utf-8 -*-
"""
Кто лучше выбирает ЦЕЛЬ: модель или механическое правило?

ВОПРОС ВЛАДЕЛЬЦА 24.09.2026: модель видит скопления ликвидности и ставит
тейк туда; код сам бы так не сделал. Значит ли это, что выбор цели — её
сильная сторона?

ПРОВЕРКА. У каждого принятого плана берём вход, стоп и цель модели, а
рядом — цели, которые выбрало бы механическое правило ПО ТОЙ ЖЕ таблице
уровней:

    ближний        — первый уровень по ходу сделки, любой;
    ликвидность    — ближний уровень с подписью «край ликвидаций»;
    пул            — ближний «скопление / равные экстремумы».

Дальше одинаково прогоняем каждую по часовым свечам: лимит на вход живёт
12 ч, потом до стопа или цели, стоп внутри свечи считается раньше цели.
Считаем долю достигнутых целей и сумму в R.

ЧЕСТНОСТЬ. Симулятор оптимистичен (на принятых планах даёт +3.6R против
−13.0R в жизни, знак совпал у 14 из 20) и не воспроизводит условие входа.
Но он ОДИНАКОВ для всех четырёх вариантов, поэтому сравнение между ними
осмысленно, даже если абсолютные числа завышены.
"""
import sys, os, csv, json, re, collections, datetime

sys.path.insert(0, '/opt/kraken/code/Live_Bot')
os.chdir('/opt/kraken/code/Live_Bot')

import numpy as np
import exchange

ENTRY_TTL_H, WATCH_H = 12, 168


def parse(row):
    raw = row.get('raw') or ''
    if '</think>' in raw:
        raw = raw.split('</think>', 1)[1]
    try:
        d = json.loads(raw.strip())
    except Exception:
        return None
    if (d.get('d') or '') != 'enter':
        return None
    try:
        lv = json.loads(row.get('levels') or '[]')
    except Exception:
        return None
    table = [{'id': a, 'price': float(b), 'kind': k} for a, b, k in lv]
    by_id = {x['id']: x['price'] for x in table}

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
    tps = [price(t) for t in (d.get('tp') or []) if price(t)]
    if side not in ('LONG', 'SHORT') or not e or not s or not tps:
        return None
    if (side == 'LONG' and not s < e < tps[0]) or (side == 'SHORT' and not s > e > tps[0]):
        return None
    return side, e, s, tps[0], table


def pick(table, side, entry, kinds=None):
    """Ближний уровень по ходу сделки, при желании только нужного вида."""
    out = []
    for x in table:
        p = x['price']
        if side == 'LONG' and p <= entry:
            continue
        if side == 'SHORT' and p >= entry:
            continue
        if kinds and not any(k in (x['kind'] or '') for k in kinds):
            continue
        out.append(p)
    if not out:
        return None
    return min(out) if side == 'LONG' else max(out)


def run(df, ts, side, entry, stop, tp):
    t = np.asarray(df['timestamp'].values).astype('datetime64[ms]').astype('int64')
    i0 = int(np.searchsorted(t, ts, side='right'))
    if i0 >= len(df) or not tp:
        return None
    hi, lo = df['high'].values.astype(float), df['low'].values.astype(float)
    R = abs(entry - stop)
    if not R:
        return None
    rr = abs(tp - entry) / R
    filled = None
    for i in range(i0, min(i0 + ENTRY_TTL_H, len(df))):
        if (side == 'LONG' and lo[i] <= entry) or (side == 'SHORT' and hi[i] >= entry):
            filled = i
            break
    if filled is None:
        return 0.0
    for i in range(filled, min(filled + WATCH_H, len(df))):
        s_hit = (lo[i] <= stop) if side == 'LONG' else (hi[i] >= stop)
        t_hit = (hi[i] >= tp) if side == 'LONG' else (lo[i] <= tp)
        if s_hit:
            return -1.0
        if t_hit:
            return rr
    return None


def main():
    rows = list(csv.DictReader(open('/opt/kraken/bot_data/llm_calls.csv', encoding='utf-8')))
    plans = [r for r in rows if r['decision'] == 'enter']
    frames = {}
    res = collections.defaultdict(list)
    dist = collections.defaultdict(list)

    for r in plans:
        p = parse(r)
        if not p:
            continue
        side, e, s, tp_model, table = p
        pair = r['pair']
        if pair not in frames:
            try:
                frames[pair] = exchange.fetch_ohlcv('1h', limit=500, symbol=pair)
            except Exception:
                frames[pair] = None
        df = frames[pair]
        if df is None:
            continue
        ts = int(datetime.datetime.fromisoformat(
            r['at'].replace('Z', '+00:00')).timestamp() * 1000)

        variants = {
            'модель': tp_model,
            'ближний': pick(table, side, e),
            'ликвидность': pick(table, side, e, ['край ликвидаций']),
            'пул': pick(table, side, e, ['скопление', 'равные экстремумы']),
        }
        for name, tp in variants.items():
            if not tp:
                continue
            out = run(df, ts, side, e, s, tp)
            if out is None:
                continue
            res[name].append(out)
            dist[name].append(abs(tp - e) / abs(e - s))

    print()
    print('%-14s %7s %8s %8s %9s %10s' % ('цель', 'планов', 'дошло', 'стоп', 'не вошли', 'ИТОГО R'))
    for name in ('модель', 'ближний', 'ликвидность', 'пул'):
        v = res.get(name) or []
        if not v:
            continue
        won = sum(1 for x in v if x > 0)
        lost = sum(1 for x in v if x < 0)
        print('%-14s %7d %8d %8d %9d %+10.2f'
              % (name, len(v), won, lost, len(v) - won - lost, sum(v)))
    print()
    print('%-14s %s' % ('цель', 'как далеко стояла, в R (медиана)'))
    for name in ('модель', 'ближний', 'ликвидность', 'пул'):
        d = sorted(dist.get(name) or [])
        if d:
            print('%-14s %.2f R' % (name, d[len(d) // 2]))


main()
