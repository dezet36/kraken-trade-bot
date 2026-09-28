"""
Вопрос v3.1 против v3 на истории (28.09.2026, docs/ИИ_замечания_на_проверку.md, п. 70).

Модель пропускала шорты B3: ключ ответа "buy" противоречит шорту («my allowed
actions are limited to buying»), а BTC за 30 дней — условие B3 и его
«лучше/хуже» — в вопросе не показан. v3.1 чинит только вопрос: тетрадь,
закономерности и исполнение те же. Условия приёмки записаны до прогона (п. 70).
test не трогаем: только train и valid.

    python research/ai_question_v31.py dry       # сколько срезов, без модели
    python research/ai_question_v31.py           # задания на сервер, ответы, итог
    python research/ai_question_v31.py collect   # ответы уже на сервере — забрать и посчитать
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_model_trader_bt as B                        # noqa: E402

TAG = 'v31_check'

# Тексты v3 — застывшие копии: стенд (ai_model_trader_bt) с 28.09.2026 несёт v3.2, а прогон v3.1
# обязан воспроизводиться с тем, что спрашивали тогда.
NOTEBOOK_V3 = """YOU ARE THE TRADER of a crypto futures bot (30 liquid USDT perpetuals, long and short).
Below is YOUR NOTEBOOK: patterns you found and verified on 2022-01..2024-06 hourly data, results after costs.
R = profit in units of the risk taken (stop distance). You decide which alerts to trade.

P1. CASCADE BOUNCE (long, hold 24h, stop = 1.0 x the coin's average daily range)
   Trigger: 4 or more of the 20 coins fell more than 4.3% within 4 hours while their open interest
   dropped more than 4.7% - leveraged longs liquidated across the market. Buy the dropped coins.
   Result: +0.157R per trade over 892 trades, 58% winners.
   Better: many coins dumping at once; 24h volume above 1.5x normal (+0.15..0.22R); BTC down >2% in 24h
           (+0.11..0.18R); coin down >25% in 7 days (+0.28R); funding above +1 bp (+0.23R).
   Worse:  a single coin dumping alone (-0.11R, usually its own bad news); BTC almost flat while coins dump
           (-0.12R); normal volume (~0R); the 2022 bear market after FTX (~0R, cascades kept going).

P2. BTC DIVERGENCE ACCUMULATION (long, hold 48h, stop = 1.3 x the coin's average daily range)
   Trigger: the coin is 2.5%+ stronger than BTC over 24h while BTC fell 1%+, open interest grew 4%+ in 24h
   and 24h volume is above 1.5x normal - someone accumulates the coin while the market is weak.
   Result: +0.111R per trade over 770 trades, 47% winners (winners are bigger than losers).
   Better: BTC up more than 3.4% over 7 days (+0.25R); BTC down more than 2.1% today (+0.19R);
           coin up more than 16% over 7 days (+0.21R); 24h volume above 2.8x normal (+0.22R).
   Worse:  BTC down over 7 days (~0R) - in a falling market the strength usually fades.

B3. DISTRIBUTION ON MARKET BOUNCE (short, hold 48h, stop = 1.3 x the coin's average daily range)
   Trigger: BTC is down over 30 days (falling market), BTC bounced 1%+ in 24h, but the coin is 2.5%+ weaker
   than BTC, while its open interest grew 4%+ and 24h volume is above 1.5x normal - someone is selling the coin
   into the market bounce. Sell it.
   Result: +0.255R per trade over 62 trades, 61% winners; positive in 6 of 7 half-years.
   Better: retail long share high (+0.35R, the crowd is buying the bounce); funding not positive (+0.38R);
           BTC 30d decline mild, above -9% (+0.38R).
   Worse:  BTC 30d decline deeper than -9% (+0.13R); very high volume above 2.1x normal (+0.12R).

RISK: at most 6 open positions. Positions opened together in one market move are one bet - prefer the
strongest alerts, and skip when your notebook says the conditions are the weak ones.
"""

QUESTION_V3 = """It is {time} UTC. New alerts this hour: {n}.
Already holding: {held}. Free position slots: {free}.

MARKET: BTC {btc4:+.1f}% in 4h, {btc24:+.1f}% in 24h, {btc7:+.1f}% in 7d. Coins in a liquidation cascade in the last 3h: {breadth}.

ALERTS - pattern, coin, then: change 4h %, change 24h %, change 7d %, strength vs BTC 24h %, open interest 4h % / 24h %,
24h volume vs normal, aggressive-buy share 24h, funding bp, retail long share percentile 30d:
{table}

Which alerts do you trade now (at most {free})? Answer with JSON only:
{{"buy": ["COIN", ...], "reason": "one sentence"}}"""


def brief_v3(data, t, items, held, free):
    btc = data['BTCUSDT'][1].loc[t]
    rows = []
    for p, key in items:
        f = data[p][1].loc[t]
        rows.append(f"{key} {p.replace('USDT', ''):9s} {f['ret_4h']:+6.1f} {f['ret_24h']:+6.1f} {f['ret_7d']:+6.1f} "
                    f"{f['rel_24h']:+6.1f} {f['oi_chg_4h']:+5.1f}/{f['oi_chg_24h']:+5.1f} {f['vol_z']:5.2f} "
                    f"{f['taker_24h']:5.3f} {f['funding_bp']:+5.2f} {f['buy_ratio_pct_30d']:.2f}")
    return QUESTION_V3.format(time=(t + B.pd.Timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'), n=len(items),
                              held=', '.join(p.replace('USDT', '') for p in held) or 'nothing', free=free,
                              btc4=btc['btc_ret_4h'], btc24=btc['btc_ret_24h'], btc7=btc['btc_ret_7d'],
                              breadth=int(btc['cascade_count_3h']), table='\n'.join(rows))


SAMPLE_P = 40
RESULT = os.path.join(HERE, 'results', 'ai_question_v31.txt')

QUESTION_V31 = """It is {time} UTC. New alerts this hour: {n}.
Already holding: {held}. Free position slots: {free}.

MARKET: BTC {btc4:+.1f}% in 4h, {btc24:+.1f}% in 24h, {btc7:+.1f}% in 7d, {btc30:+.1f}% in 30d. Coins in a liquidation cascade in the last 3h: {breadth}.

ALERTS - pattern, side, coin, then: change 4h %, change 24h %, change 7d %, strength vs BTC 24h %, open interest 4h % / 24h %,
24h volume vs normal, aggressive-buy share 24h, funding bp, retail long share percentile 30d:
{table}

Every alert already meets its pattern's trigger. The side of each alert is fixed by its pattern (long = buy the coin, short = sell it).
Which alerts do you trade now (at most {free})? Answer with JSON only:
{{"trade": ["COIN", ...], "reason": "one sentence"}}"""


def brief_v31(data, t, items, held, free, template=None):
    btc = data['BTCUSDT'][1].loc[t]
    rows = []
    for p, key in items:
        f = data[p][1].loc[t]
        rows.append(f"{key} {B.PATTERNS[key]['side']:5s} {p.replace('USDT', ''):9s} {f['ret_4h']:+6.1f} "
                    f"{f['ret_24h']:+6.1f} {f['ret_7d']:+6.1f} {f['rel_24h']:+6.1f} "
                    f"{f['oi_chg_4h']:+5.1f}/{f['oi_chg_24h']:+5.1f} {f['vol_z']:5.2f} "
                    f"{f['taker_24h']:5.3f} {f['funding_bp']:+5.2f} {f['buy_ratio_pct_30d']:.2f}")
    return (template or QUESTION_V31).format(time=(t + B.pd.Timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'), n=len(items),
                               held=', '.join(p.replace('USDT', '') for p in held) or 'nothing', free=free,
                               btc4=btc['btc_ret_4h'], btc24=btc['btc_ret_24h'], btc7=btc['btc_ret_7d'],
                               btc30=btc['btc_ret_30d'], breadth=int(btc['cascade_count_3h']), table='\n'.join(rows))


def picks(text, items):
    """Выбор модели: ключ "trade" (v3.1) или "buy" (v3). None — ответ без JSON."""
    try:
        body = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except Exception:                                  # noqa: BLE001
        return None
    chosen = body.get('trade')
    if chosen is None:
        chosen = body.get('buy')
    names = {p.replace('USDT', ''): p for p, _ in items}
    return [names[str(x).upper().replace('USDT', '')] for x in (chosen or [])
            if str(x).upper().replace('USDT', '') in names]


def slices(data, split):
    """Срезы как в ai_model_trader_bt.run: «держит/свободно» — по механике. Срез без свободных пар не берём."""
    evs = B.alerts(data, split)
    res = B.outcomes(data, evs)
    held, out = {}, []
    for t, items in evs:
        held = {p: e for p, e in held.items() if e > t}
        free = max(1, B.SLOTS - len(held))
        cand = [(p, k) for p, k in items if p not in held and (t, p) in res]
        if cand:
            out.append({'split': split, 't': t, 'items': cand, 'held': list(held), 'free': free})
        for p, _ in cand[:max(0, B.SLOTS - len(held))]:
            held[p] = res[(t, p)][1]
    return out, res


def sets():
    """Данные и наборы срезов S_B3, S_P (п. 70) — одни и те же для всех версий вопроса."""
    data = B.prepare()
    all_slices, res = [], {}
    for split in ('train', 'valid'):
        s, r = slices(data, split)
        all_slices += s
        res.update(r)
    s_b3 = [s for s in all_slices if any(k == 'B3' for _, k in s['items'])]
    only_p = [s for s in all_slices if all(k != 'B3' for _, k in s['items'])]
    rng = np.random.default_rng(11)
    s_p = [only_p[i] for i in sorted(rng.choice(len(only_p), size=min(SAMPLE_P, len(only_p)), replace=False))]
    return data, all_slices, s_b3, only_p, s_p, res


def plan():
    data, all_slices, s_b3, only_p, s_p, res = sets()
    jobs = ([('b3', 'v31', s) for s in s_b3] + [('p', 'v3', s) for s in s_p] + [('p', 'v31', s) for s in s_p])
    prompts = [(brief_v3 if v == 'v3' else brief_v31)(data, s['t'], s['items'], s['held'], s['free'])
               for _, v, s in jobs]
    n_b3 = sum(1 for s in s_b3 for _, k in s['items'] if k == 'B3')
    print(f'срезов: всего {len(all_slices)}, с B3 {len(s_b3)} (сигналов B3 {n_b3}), только P1/P2 {len(only_p)}, '
          f'выборка S_P {len(s_p)}; вопросов модели {len(prompts)}', flush=True)
    return jobs, prompts, res


def mean(x):
    return float(np.mean(x)) if len(x) else float('nan')


def evaluate(jobs, answers, res, cand='v31', name='v3.1', result=None, title=None):
    """Условия п. 70 для версии-кандидата cand; основа на S_P — ответы v3. Ответов меньше, чем заданий, — считаем что есть."""
    lines = []

    def say(text=''):
        print(text, flush=True)
        lines.append(text)

    stats = {}
    for (kind, version, s), text in zip(jobs, answers):
        chosen = picks(text, s['items'])
        st = stats.setdefault((kind, version), {'n': 0, 'bad': 0, 'shown': {}, 'taken': {}, 'r_all': {}, 'r_taken': {}})
        st['n'] += 1
        if chosen is None:
            st['bad'] += 1
            chosen = []
        chosen = chosen[:s['free']]
        for p, k in s['items']:
            r = res[(s['t'], p)][0]
            st['shown'][k] = st['shown'].get(k, 0) + 1
            st['r_all'].setdefault(k, []).append(r)
            if p in chosen:
                st['taken'][k] = st['taken'].get(k, 0) + 1
                st['r_taken'].setdefault(k, []).append(r)

    def rate(st, keys):
        shown = sum(st['shown'].get(k, 0) for k in keys)
        return sum(st['taken'].get(k, 0) for k in keys) / shown if shown else float('nan')

    def r_of(st, part, keys):
        return mean([r for k in keys for r in st[part].get(k, [])])

    say(title or f'Вопрос {name} против v3 на train + valid (п. 70)')
    for (kind, version), st in sorted(stats.items()):
        parts = ', '.join(f"{k}: взято {st['taken'].get(k, 0)}/{st['shown'][k]} "
                          f"R взятых {r_of(st, 'r_taken', [k]):+.3f} (всех {r_of(st, 'r_all', [k]):+.3f})"
                          for k in sorted(st['shown']))
        say(f"  {'S_B3' if kind == 'b3' else 'S_P '} {version:4s} срезов {st['n']:3d}, без JSON {st['bad']}: {parts}")

    b3 = stats.get(('b3', cand))
    p_old, p_new = stats.get(('p', 'v3')), stats.get(('p', cand))
    if not (b3 and p_old and p_new):
        say(f'\nНе хватает ответов для условий: S_B3 {cand} {bool(b3)}, S_P v3 {bool(p_old)}, S_P {cand} {bool(p_new)}')
        write(lines, result)
        return None
    c1 = rate(b3, ['B3'])
    c2 = r_of(b3, 'r_taken', ['B3']) - r_of(b3, 'r_all', ['B3'])
    c3a = abs(rate(p_new, ['P1', 'P2']) - rate(p_old, ['P1', 'P2']))
    c3b = r_of(p_new, 'r_taken', ['P1', 'P2']) - r_of(p_old, 'r_taken', ['P1', 'P2'])
    c4 = (b3['bad'] + p_new['bad']) / (b3['n'] + p_new['n'])
    checks = [(f'1. {name} берёт B3: {c1 * 100:.0f}% (нужно ≥ 50%)', c1 >= 0.5),
              (f'2. R взятых B3 − R всех B3: {c2:+.3f} (нужно ≥ −0.10)', c2 >= -0.10),
              (f'3. S_P: доля взятых P1/P2 {name} − v3: {c3a * 100:.0f} п. п. (нужно ≤ 15); '
               f'R взятых {name} − v3: {c3b:+.3f} (нужно ≥ −0.10)', c3a <= 0.15 and c3b >= -0.10),
              (f'4. без JSON у {name}: {c4 * 100:.1f}% (нужно ≤ 5%)', c4 <= 0.05)]
    say()
    for text, ok in checks:
        say(f"  {'да ' if ok else 'НЕТ'} {text}")
    verdict = all(ok for _, ok in checks)
    say(f"\nИтог: {name} {'принят — идёт вживую' if verdict else 'не принят — остаётся v3'}")
    write(lines, result)
    return verdict


def write(lines, result=None):
    result = result or RESULT
    os.makedirs(os.path.dirname(result), exist_ok=True)
    with open(result, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    mode = sys.argv[1] if len(sys.argv) > 1 else 'run'
    jobs, prompts, res = plan()
    if mode == 'dry':
        print(prompts[0])
        sys.exit(0)
    B.NOTEBOOK = NOTEBOOK_V3                          # v3.1 спрашивали с тетрадью v3
    answers = B.collect(TAG, len(prompts)) if mode == 'collect' else B.ask_model(prompts, TAG)
    evaluate(jobs, answers, res)
