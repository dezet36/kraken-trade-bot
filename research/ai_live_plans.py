"""
Живые планы ИИ — повтор по свечам Bybit так, как их исполняет бот, и те же
планы с другим исполнением.

ЗАЧЕМ. По двадцати сделкам нельзя понять, что именно не так: направление,
место входа, стоп или цель. Принятых планов больше, чем сделок (бот держит
одну позицию на пару, ждёт кулдаун), и каждый план можно довести до исхода
по свечам: так видно, что дало бы другое исполнение ТЕХ ЖЕ планов, и
отдельно — чего стоит само направление модели.

Жизнь плана как в strategy_llm / paper_broker:
  готов через `seconds` после разметки; 'now' — заявка сразу; иначе условие по
  закрытым часовым свечам до 12 ч (strategy_llm.condition_met); цель до входа —
  план снят; вход за ценой — по рынку, тейкером; иначе лимит живёт 12 ч, цель
  до налива — заявка снята. Позиция по 5m: безубыток при +1R раньше стопа в
  той же свече, стоп раньше целей, 50/50 на двух целях, безубыток после
  первой. Издержки как у бумажного брокера.
Повтор сверен с журналом 26.09.2026: 16 из 20 сделок совпали по минуте входа
(±15 мин) и исходу; расхождения — планы 19–20.09, исполненные по старым
правилам (срок заявки 72 ч) или из другого плана той же пары.

Данные — копия с сервера (в git не кладутся):
    scp -P 53547 root@195.133.35.5:/opt/kraken/bot_data/{llm_calls.csv,paper_trades.jsonl} <папка>
Свечи докачиваются с Bybit в <папка>/candles.

Запуск:
    python research/ai_live_plans.py <папка с данными>
"""
import bisect
import csv
import json
import os
import pickle
import re
import statistics as st
import sys
import time
from collections import Counter
from datetime import datetime, timezone

H = 3_600_000
M5 = 300_000
TTL = 12 * H
FEE_M, FEE_T, SLIP, BE_OFF = 0.0002, 0.00055, 0.0005, 0.00175
TF_MS = {'5m': M5, '1h': H, '4h': 4 * H, '1d': 24 * H}

csv.field_size_limit(10 ** 8)
DATA = None
_cache = {}


def ts(iso):
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%m-%d %H:%M')


# ── Свечи Bybit с кэшем на диске ─────────────────────────────────────────────
def _fetch(pair, tf, since_ms, until_ms):
    import ccxt
    ex = ccxt.bybit({'options': {'defaultType': 'swap'}, 'enableRateLimit': True})
    rows, cursor = {}, since_ms
    while cursor < until_ms:
        page = []
        for attempt in range(4):
            try:
                page = ex.fetch_ohlcv(pair[:-4] + '/USDT:USDT', tf, since=cursor, limit=1000)
                break
            except Exception:                              # noqa: BLE001
                time.sleep(2 + attempt * 2)
        if not page:
            break
        for c in page:
            rows[c[0]] = c
        nxt = page[-1][0] + TF_MS[tf]
        if nxt <= cursor:
            break
        cursor = nxt
    now = int(time.time() * 1000)
    return [c for c in sorted(rows.values()) if c[0] + TF_MS[tf] <= now]


def bars(pair, tf):
    """Закрытые свечи пары: (строки [ts,o,h,l,c,v], ключи ts). Кэш — DATA/candles."""
    key = (pair, tf)
    if key in _cache:
        return _cache[key]
    folder = os.path.join(DATA, 'candles')
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'{pair}_{tf}.pkl')
    rows = pickle.load(open(path, 'rb')) if os.path.exists(path) else []
    need_from = _first_plan_ms - (60 * 24 * H if tf != '5m' else 24 * H)
    now = int(time.time() * 1000)
    if not rows or rows[0][0] > need_from or rows[-1][0] < now - 3 * TF_MS[tf]:
        rows = _fetch(pair, tf, need_from, now)
        with open(path, 'wb') as fh:
            pickle.dump(rows, fh)
    _cache[key] = (rows, [r[0] for r in rows])
    return _cache[key]


def close_at(pair, t):
    rows, keys = bars(pair, '5m')
    i = bisect.bisect_right(keys, t - M5) - 1
    return rows[i][4] if i >= 0 else None


def five(pair, t0, t1):
    rows, keys = bars(pair, '5m')
    return rows[bisect.bisect_left(keys, t0):bisect.bisect_left(keys, t1)]


def day_range_pct(pair, t0, days=14):
    rows, keys = bars(pair, '1d')
    i = bisect.bisect_right(keys, t0 - 24 * H)
    hist = rows[max(0, i - days):i]
    return sum((r[2] - r[3]) / r[4] * 100 for r in hist) / len(hist) if hist else None


def change_pct(pair, t0, hours):
    rows, keys = bars(pair, '1h')
    i = bisect.bisect_right(keys, t0 - H) - 1
    if i - hours < 0:
        return None
    return (rows[i][4] / rows[i - hours][4] - 1) * 100


# ── Планы из журнала вызовов ─────────────────────────────────────────────────
def answer(raw):
    text = str(raw).split('</think>')[-1]
    m = re.search(r'\{[\s\S]*\}', text)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def price_ref(ref, levels):
    s = str(ref or '')
    m = re.search(r'\(([-0-9.eE]+)\)', s)
    if m:
        return float(m.group(1))
    m = re.match(r'\s*(L\d+)', s)
    if m and m.group(1) in levels:
        return levels[m.group(1)]
    try:
        return float(s)
    except ValueError:
        return None


_first_plan_ms = 0


def load_plans():
    global _first_plan_ms
    rows = list(csv.DictReader(open(os.path.join(DATA, 'llm_calls.csv'), encoding='utf-8')))
    out = []
    for r in rows:
        if r['decision'] != 'enter' or r['gate']:
            continue
        a = answer(r['raw']) or {}
        try:
            levels = {x[0]: float(x[1]) for x in json.loads(r.get('levels') or '[]')}
        except ValueError:
            levels = {}
        trig = a.get('trigger') if isinstance(a.get('trigger'), dict) else {}
        when = (trig.get('when') or 'now').strip()
        side = r['side'] or a.get('side')
        entry, stop = float(r['entry']), float(r['stop'])
        long_ = side == 'LONG'
        tps = [price_ref(x, levels) for x in (a.get('tp') or [])]
        tps.append(float(r['tp1']) if r.get('tp1') else None)
        tps = sorted({t for t in tps if t and ((t > entry) if long_ else (t < entry))},
                     key=lambda t: abs(t - entry))
        if not tps:
            continue
        level = price_ref(trig.get('level'), levels)
        if level is None and when != 'now':
            # Планы до 21.09 без таблицы уровней в журнале: уровень условия —
            # тот же id, что у входа или стопа, иначе первое число из записки.
            lid = re.match(r'\s*(L\d+)', str(trig.get('level') or ''))
            eid = re.match(r'\s*(L\d+)', str(a.get('entry') or ''))
            sid = re.match(r'\s*(L\d+)', str(a.get('stop') or ''))
            if lid and eid and lid.group(1) == eid.group(1):
                level = entry
            elif lid and sid and lid.group(1) == sid.group(1):
                level = float(r['inval']) if r.get('inval') else stop
            else:
                nums = re.findall(r'\d+\.\d+', str(trig.get('note') or '') + ' ' + str(r.get('trigger') or ''))
                level = float(nums[0]) if nums else None
        out.append({
            'at': r['at'], 'pair': r['pair'], 'side': side, 'long': long_,
            't_ready': ts(r['at']) + int(float(r['seconds'] or 0) * 1000),
            'when': when, 'level': level, 'entry': entry, 'stop': stop, 'tps': tps[:2],
            'bias': a.get('bias') or r.get('bias') or '', 'p': a.get('p'),
            'rr1': abs(tps[0] - entry) / abs(entry - stop),
            'stop_pct': abs(entry - stop) / entry * 100,
        })
    _first_plan_ms = min(p['t_ready'] for p in out) if out else 0
    return out


# ── Условие, заявка, позиция ─────────────────────────────────────────────────
def hourly_after(pair, t0, t1):
    rows, keys = bars(pair, '1h')
    out = []
    for c in rows[bisect.bisect_right(keys, t0 - H):]:
        if c[0] + H <= t0:
            continue
        if c[0] + H > t1:
            break
        out.append(c)
    return out


def median_vol(pair, t_open):
    rows, keys = bars(pair, '1h')
    i = bisect.bisect_left(keys, t_open)
    vols = [c[5] for c in rows[max(0, i - 48):i]]
    return st.median(vols) if vols else 0.0


def condition(when, level, long_, seq, med):
    """strategy_llm.condition_met по часовым [ts,o,h,l,c,v]."""
    if level is None:
        return False
    if when in ('close_above', 'close_above_with_volume'):
        return any(c[4] > level and (when == 'close_above' or (med and c[5] >= 1.5 * med)) for c in seq)
    if when in ('close_below', 'close_below_with_volume'):
        return any(c[4] < level and (when == 'close_below' or (med and c[5] >= 1.5 * med)) for c in seq)
    if when == 'retest':
        broke = False
        for c in seq:
            if not broke:
                broke = c[4] > level if long_ else c[4] < level
                continue
            touched = c[3] <= level if long_ else c[2] >= level
            held = c[4] >= level if long_ else c[4] <= level
            if touched and held:
                return True
            if (c[4] < level) if long_ else (c[4] > level):
                broke = False
        return False
    if when == 'sweep_reclaim':
        swept = None
        for i, c in enumerate(seq):
            beyond = c[3] < level if long_ else c[2] > level
            if swept is None and beyond:
                swept = i
            if swept is not None and i - swept <= 3:
                if (c[4] > level) if long_ else (c[4] < level):
                    return True
            elif swept is not None and i - swept > 3:
                swept = i if beyond else None
        return False
    return False


def arm(plan):
    """-> (момент заявки, цена в этот момент, причина снятия или '')."""
    pair, long_, tp1, t0 = plan['pair'], plan['long'], plan['tps'][0], plan['t_ready']
    if plan['when'] == 'now':
        return t0, close_at(pair, t0), ''
    seq = hourly_after(pair, t0, t0 + TTL)
    for k, c in enumerate(seq):
        closed = c[0] + H
        if any((b[2] >= tp1) if long_ else (b[3] <= tp1) for b in five(pair, t0, closed)):
            return None, None, 'цель без входа'
        if condition(plan['when'], plan['level'], long_, seq[:k + 1], median_vol(pair, c[0])):
            return closed, c[4], ''
    return None, None, 'условие не наступило'


def manage(plan, fill_t, fill, entry_fee, stop, targets, fractions, be_at_1r=True, horizon_h=336):
    long_ = plan['long']
    risk = abs(fill - stop)
    if risk <= 0:
        return None
    be_level = fill + risk if long_ else fill - risk
    seg = five(plan['pair'], fill_t - fill_t % M5, fill_t + horizon_h * H)
    size, realized, fees = 1.0, 0.0, entry_fee * fill
    cur, be_set, hit, mfe = stop, False, 0, 0.0
    sign = 1 if long_ else -1
    for b in seg:
        hi, lo = b[2], b[3]
        mfe = max(mfe, ((hi - fill) if long_ else (fill - lo)) / risk)
        if be_at_1r and not be_set and ((hi >= be_level) if long_ else (lo <= be_level)):
            cur = fill * (1 + BE_OFF) if long_ else fill * (1 - BE_OFF)
            be_set = True
        if (lo <= cur) if long_ else (hi >= cur):
            px = cur * (1 - SLIP) if long_ else cur * (1 + SLIP)
            realized += sign * (px - fill) * size
            fees += FEE_T * px * size
            return {'r': (realized - fees) / risk, 'reason': 'BE' if be_set else 'SL',
                    'hours': (b[0] - fill_t) / H, 'mfe_r': mfe}
        while hit < len(targets):
            lvl = targets[hit]
            if not ((hi >= lvl) if long_ else (lo <= lvl)):
                break
            part = fractions[hit] if hit < len(targets) - 1 else size
            realized += sign * (lvl - fill) * part
            fees += FEE_M * lvl * part
            size -= part
            hit += 1
            if hit < len(targets):
                if not be_set:
                    cur, be_set = fill, True
            else:
                return {'r': (realized - fees) / risk, 'reason': f'TP{hit}',
                        'hours': (b[0] - fill_t) / H, 'mfe_r': mfe}
    if not seg:
        return None
    last = seg[-1][4]
    realized += sign * (last - fill) * size
    fees += FEE_T * last * size
    return {'r': (realized - fees) / risk, 'reason': 'OPEN', 'hours': (seg[-1][0] - fill_t) / H, 'mfe_r': mfe}


def run(plan, variant):
    """
    variant:
      entry: 'plan' | 'market'  — условие и лимит как у бота | по рынку в момент готовности
      targets: 'plan' | k       — цели модели 50/50 | одна цель на k·R
      be: True/False            — безубыток при +1R
      side: None | 'LONG'       — сторона принудительно (цена направления модели)
    """
    long_ = plan['long'] if not variant.get('side') else variant['side'] == 'LONG'
    p = dict(plan, long=long_)
    if variant.get('entry') == 'market' or variant.get('side'):
        t_fill = p['t_ready']
        price = close_at(p['pair'], t_fill)
        if price is None:
            return {'status': 'нет данных'}
        fill = price * (1 + SLIP) if long_ else price * (1 - SLIP)
        dist = plan['stop_pct'] / 100
        stop = fill * (1 - dist) if long_ else fill * (1 + dist)
        k = variant.get('targets', 'plan')
        k = plan['rr1'] if k == 'plan' else k
        risk = abs(fill - stop)
        res = manage(p, t_fill, fill, FEE_T, stop, [fill + k * risk if long_ else fill - k * risk], [1.0],
                     variant.get('be', True))
        return dict(res or {}, status='вход' if res else 'нет данных')
    t_order, price, why = arm(p)
    if t_order is None:
        return {'status': why}
    entry = plan['entry']
    marketable = (entry >= price) if long_ else (entry <= price)
    if marketable:
        fill_t, fill, fee = t_order, (price * (1 + SLIP) if long_ else price * (1 - SLIP)), FEE_T
    else:
        fill_t = None
        for b in five(p['pair'], t_order, t_order + TTL):
            if (b[3] <= entry) if long_ else (b[2] >= entry):
                fill_t = b[0]
                break
            if (b[2] >= plan['tps'][0]) if long_ else (b[3] <= plan['tps'][0]):
                return {'status': 'цель до налива'}
        if fill_t is None:
            return {'status': 'не налилась'}
        fill, fee = entry, FEE_M
    stop = plan['stop']
    if (fill <= stop) if long_ else (fill >= stop):
        return {'status': 'вход за стопом'}
    k = variant.get('targets', 'plan')
    risk = abs(fill - stop)
    if k == 'plan':
        targets = [t for t in plan['tps'] if ((t > fill) if long_ else (t < fill))]
        if not targets:
            return {'status': 'цель за входом'}
        fractions = [0.5, 0.5] if len(targets) == 2 else [1.0]
    else:
        targets, fractions = [fill + k * risk if long_ else fill - k * risk], [1.0]
    res = manage(p, fill_t, fill, fee, stop, targets, fractions, variant.get('be', True))
    return dict(res or {}, status='вход' if res else 'нет данных', fill_t=fill_t, marketable=marketable)


def summary(results, label):
    done = [r for r in results if r.get('status') == 'вход' and 'r' in r]
    if not done:
        print(f'  {label:50s} сделок 0')
        return
    rs = [r['r'] for r in done]
    wins = sum(1 for x in rs if x > 0.05)
    stops = sum(1 for r in done if r['reason'] == 'SL')
    print(f'  {label:50s} сделок {len(done):3d}  в плюс {wins / len(done) * 100:3.0f}%  '
          f'стопов {stops / len(done) * 100:3.0f}%  сумма {sum(rs):+7.2f}R  в среднем {st.mean(rs):+.3f}R')


def main(folder):
    global DATA
    DATA = folder
    plans = load_plans()
    print(f'{len(plans)} принятых планов: лонгов {sum(p["long"] for p in plans)}, '
          f'шортов {sum(not p["long"] for p in plans)}')

    trades = [t for t in (json.loads(line) for line in open(os.path.join(DATA, 'paper_trades.jsonl'), encoding='utf-8'))
              if t['strategy'] == 'LLM']
    # Безубыток при +1R появился 23.09.2026 08:07 UTC (4a7450c): сверка — по
    # правилам, действовавшим на момент плана.
    be_since = ts('2026-09-23T08:07:00+00:00')
    same = 0
    for t in trades:
        to = ts(t['open_time'])
        cands = [p for p in plans if p['pair'] == t['pair'] and p['side'] == t['direction']
                 and p['t_ready'] - 30 * 60_000 <= to]
        if not cands:
            continue
        r = run(cands[-1], {'be': cands[-1]['t_ready'] > be_since})
        if r.get('status') == 'вход' and r.get('reason') == t['exit_reason'] \
                and abs(r.get('fill_t', 0) - to) <= 15 * 60_000:
            same += 1
    print(f'сверка с журналом: {same} из {len(trades)} сделок совпали по входу и исходу\n')

    base = [run(p, {}) for p in plans]
    print('исходы планов:', dict(Counter(r['status'] for r in base)))
    print('\nТЕ ЖЕ ПЛАНЫ, ДРУГОЕ ИСПОЛНЕНИЕ')
    summary(base, 'как бот сейчас (условие, лимит, БУ +1R, 50/50)')
    summary([run(p, {'be': False}) for p in plans], 'без безубытка')
    for k in (1.0, 1.5, 2.0):
        summary([run(p, {'targets': k}) for p in plans], f'одна цель {k}R')
    print('\nСТОРОНА')
    summary([r for r, p in zip(base, plans) if p['long']], 'только лонги')
    summary([r for r, p in zip(base, plans) if not p['long']], 'только шорты')
    summary([run(p, {'entry': 'market'}) for p in plans], 'по рынку в момент плана, геометрия плана')
    summary([run(p, {'side': 'LONG'}) for p in plans], 'то же, но всегда ЛОНГ (цена направления)')

    done = [(p, r) for p, r in zip(plans, base) if r.get('status') == 'вход']
    if done:
        ranges = [day_range_pct(p['pair'], p['t_ready']) or 0 for p in plans]
        print('\nГЕОМЕТРИЯ ПРИНЯТЫХ ПЛАНОВ')
        print(f'  стоп: медиана {st.median(p["stop_pct"] for p in plans):.2f}% — '
              f'{st.median(p["stop_pct"] / d for p, d in zip(plans, ranges) if d):.2f} среднего дневного размаха')
        print(f'  первая цель: медиана {st.median(p["rr1"] for p in plans):.2f}R — '
              f'{st.median(p["stop_pct"] * p["rr1"] / d for p, d in zip(plans, ranges) if d):.2f} дневного размаха; '
              f'дальше размаха у {sum(1 for p, d in zip(plans, ranges) if d and p["stop_pct"] * p["rr1"] > d) / len(plans) * 100:.0f}% планов')
        ps = Counter(p['p'] for p in plans)
        print(f'  p модели: {dict(ps.most_common(4))} — доля первой цели раньше стопа у сделок '
              f'{sum(1 for p, r in done if r["reason"].startswith("TP") or r["r"] > 0.5) / len(done) * 100:.0f}%')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                             'sim-data', 'ai'))
