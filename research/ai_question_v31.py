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


def brief_v31(data, t, items, held, free):
    btc = data['BTCUSDT'][1].loc[t]
    rows = []
    for p, key in items:
        f = data[p][1].loc[t]
        rows.append(f"{key} {B.PATTERNS[key]['side']:5s} {p.replace('USDT', ''):9s} {f['ret_4h']:+6.1f} "
                    f"{f['ret_24h']:+6.1f} {f['ret_7d']:+6.1f} {f['rel_24h']:+6.1f} "
                    f"{f['oi_chg_4h']:+5.1f}/{f['oi_chg_24h']:+5.1f} {f['vol_z']:5.2f} "
                    f"{f['taker_24h']:5.3f} {f['funding_bp']:+5.2f} {f['buy_ratio_pct_30d']:.2f}")
    return QUESTION_V31.format(time=(t + B.pd.Timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'), n=len(items),
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


def plan():
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
    jobs = ([('b3', 'v31', s) for s in s_b3] + [('p', 'v3', s) for s in s_p] + [('p', 'v31', s) for s in s_p])
    prompts = [(B.brief if v == 'v3' else brief_v31)(data, s['t'], s['items'], s['held'], s['free'])
               for _, v, s in jobs]
    n_b3 = sum(1 for s in s_b3 for _, k in s['items'] if k == 'B3')
    print(f'срезов: всего {len(all_slices)}, с B3 {len(s_b3)} (сигналов B3 {n_b3}), только P1/P2 {len(only_p)}, '
          f'выборка S_P {len(s_p)}; вопросов модели {len(prompts)}', flush=True)
    return jobs, prompts, res


def mean(x):
    return float(np.mean(x)) if len(x) else float('nan')


def evaluate(jobs, answers, res):
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

    say('Вопрос v3.1 против v3 на train + valid (п. 70)')
    for (kind, version), st in sorted(stats.items()):
        parts = ', '.join(f"{k}: взято {st['taken'].get(k, 0)}/{st['shown'][k]} "
                          f"R взятых {r_of(st, 'r_taken', [k]):+.3f} (всех {r_of(st, 'r_all', [k]):+.3f})"
                          for k in sorted(st['shown']))
        say(f"  {'S_B3' if kind == 'b3' else 'S_P '} {version:4s} срезов {st['n']:3d}, без JSON {st['bad']}: {parts}")

    b3 = stats.get(('b3', 'v31'))
    p_old, p_new = stats.get(('p', 'v3')), stats.get(('p', 'v31'))
    c1 = rate(b3, ['B3'])
    c2 = r_of(b3, 'r_taken', ['B3']) - r_of(b3, 'r_all', ['B3'])
    c3a = abs(rate(p_new, ['P1', 'P2']) - rate(p_old, ['P1', 'P2']))
    c3b = r_of(p_new, 'r_taken', ['P1', 'P2']) - r_of(p_old, 'r_taken', ['P1', 'P2'])
    c4 = (b3['bad'] + p_new['bad']) / (b3['n'] + p_new['n'])
    checks = [(f'1. v3.1 берёт B3: {c1 * 100:.0f}% (нужно ≥ 50%)', c1 >= 0.5),
              (f'2. R взятых B3 − R всех B3: {c2:+.3f} (нужно ≥ −0.10)', c2 >= -0.10),
              (f'3. S_P: доля взятых P1/P2 v3.1 − v3: {c3a * 100:.0f} п. п. (нужно ≤ 15); '
               f'R взятых v3.1 − v3: {c3b:+.3f} (нужно ≥ −0.10)', c3a <= 0.15 and c3b >= -0.10),
              (f'4. без JSON у v3.1: {c4 * 100:.1f}% (нужно ≤ 5%)', c4 <= 0.05)]
    say()
    for text, ok in checks:
        say(f"  {'да ' if ok else 'НЕТ'} {text}")
    verdict = all(ok for _, ok in checks)
    say(f"\nИтог: {'v3.1 принят — идёт вживую' if verdict else 'v3.1 не принят — остаётся v3'}")
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return verdict


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    mode = sys.argv[1] if len(sys.argv) > 1 else 'run'
    jobs, prompts, res = plan()
    if mode == 'dry':
        print(prompts[0])
        sys.exit(0)
    answers = B.collect(TAG, len(prompts)) if mode == 'collect' else B.ask_model(prompts, TAG)
    evaluate(jobs, answers, res)
