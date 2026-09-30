"""
Модель торгует по своей тетради на исторических срезах (27.09.2026).

Тетрадь v1 — две закономерности, прошедшие отбор на train и подтверждение на
valid (docs/ИИ_замечания_на_проверку.md, п. 68), с цифрами только train
(2022-01…2024-06):
    P1  отскок после широкой разгрузки — механику нашли независимо я
        (ai_claude_hypotheses C5b) и модель (круг 2, liq_cascade_bounce_long);
    P2  накопление при расхождении с BTC — правило модели (круг 3,
        btc_divergence_accum).
Срез — час, в который у монеты сработала закономерность. Модель видит сводку по
этим монетам и рынку, что уже держит и сколько мест свободно (всего 4), и
решает, что купить, или ничего. Монету в позиции второй раз не берём.

Сравнение на тех же срезах и с теми же местами:
    модель      — её выбор;
    механика    — всё подряд в порядке: P1 по глубине падения, затем P2 по силе к BTC;
    случайно    — среднее 300 случайных выборов.
Исполнение — как в ai_pattern_lab (вход на открытии следующего часа по рынку,
издержки); стоп и срок — у каждой закономерности свои.

    python research/ai_model_trader_bt.py valid [N]    # подтверждение (N — только первые N срезов)
    python research/ai_model_trader_bt.py test         # итоговый, один раз
"""
import json
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_pattern_lab as L                            # noqa: E402
import ai_model_research as R                         # noqa: E402

OUT = os.path.join(HERE, 'results', 'model_trader')
SLOTS = 6
VERSION = 'v3.4'        # тетрадь и вопрос: имена файлов ответов не пересекаются с прошлой версией
# v3.4 (30.09.2026, docs п. 77): в тетради нет P2 (выключена), есть P4 «сжатие шортистов» — её запись
# в research/ai_question_v34.P4; PATTERNS стенда остаются P1, P2, B3 ради воспроизводимости прежних проверок.
# v3.3 (28.09.2026, docs п. 72): RISK — «бери все подходящие сигналы каскада», причина — по-русски.
# v3.2 (28.09.2026, docs п. 70): в вопросе — сторона сигнала, BTC за 30 дней, «каждый сигнал уже выполнил
# условие», ключ ответа "trade", причина ≤ 25 слов, шкала ранга доли розницы; в тетради — порог «high» у B3.

# Тетрадь v3 (28.09.2026): P1 и P2 перенесены на 10 пар сверх базовых 20 (research/ai_extra_pairs.py:
# на новых парах R > 0 и в train, и в valid). Широта разгрузки (P1) считается только по базовым 20,
# B3 на новых парах не подтвердилась — торгуется только на базовых.
EXTRA = ('BCHUSDT', 'ETCUSDT', 'ATOMUSDT', 'FILUSDT', 'TRXUSDT', 'OPUSDT', 'APTUSDT', 'INJUSDT',
         '1000PEPEUSDT', 'SEIUSDT')

PATTERNS = {
    'P1': {'label': 'CASCADE BOUNCE', 'side': 'long', 'hold': 24, 'stop': 1.0, 'rank': ('ret_4h', 1),
           'conditions': [['ret_4h', '<', -4.2604], ['oi_chg_4h', '<', -4.7381], ['cascade_count_3h', '>=', 4]]},
    'P2': {'label': 'BTC DIVERGENCE ACCUMULATION', 'side': 'long', 'hold': 48, 'stop': 1.3, 'rank': ('rel_24h', -1),
           'conditions': [['rel_24h', '>=', 2.5], ['btc_ret_24h', '<=', -1.0], ['oi_chg_24h', '>=', 4.0],
                          ['vol_z', '>=', 1.5]]},
    'B3': {'label': 'DISTRIBUTION ON MARKET BOUNCE', 'side': 'short', 'hold': 48, 'stop': 1.3, 'rank': ('rel_24h', 1),
           'universe': 'core',
           'conditions': [['btc_ret_30d', '<', 0.0], ['rel_24h', '<=', -2.5], ['btc_ret_24h', '>=', 1.0],
                          ['oi_chg_24h', '>=', 4.0], ['vol_z', '>=', 1.5]]},
}

NOTEBOOK = """YOU ARE THE TRADER of a crypto futures bot (30 liquid USDT perpetuals, long and short).
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

B3. DISTRIBUTION ON MARKET BOUNCE (short, hold 48h, stop = 1.3 x the coin's average daily range)
   Trigger: BTC is down over 30 days (falling market), BTC bounced 1%+ in 24h, but the coin is 2.5%+ weaker
   than BTC, while its open interest grew 4%+ and 24h volume is above 1.5x normal - someone is selling the coin
   into the market bounce. Sell it.
   Result: +0.255R per trade over 62 trades, 61% winners; positive in 6 of 7 half-years.
   Better: retail long share high (30-day rank above 0.5: +0.35R, the crowd is buying the bounce); funding not positive (+0.38R);
           BTC 30d decline mild, above -9% (+0.38R).
   Worse:  BTC 30d decline deeper than -9% (+0.13R); very high volume above 2.1x normal (+0.12R).

P4. SHORT SQUEEZE (long, hold 48h, stop = 1.5 x the coin's average daily range)
   Trigger: funding is deeply negative (-2 bp per 8h or lower: shorts pay longs every 8 hours) and the share of retail
   accounts in longs is at the lowest 3% of its last 30 days - the crowd is maximally short and pays to stay short.
   Buy the coin: the shorts give up or get squeezed.
   Result: +0.113R per trade over 386 trades, 50% winners (winners are bigger than losers).

RISK: at most 6 open positions. Take every alert that fits your notebook - a liquidation cascade usually gives
several good trades at once, and on history taking them all earned more than picking one or two. Skip an
alert only when your notebook says its conditions are the weak ones.
"""

QUESTION = """It is {time} UTC. New alerts this hour: {n}.
Already holding: {held}. Free position slots: {free}.

MARKET: BTC {btc4:+.1f}% in 4h, {btc24:+.1f}% in 24h, {btc7:+.1f}% in 7d, {btc30:+.1f}% in 30d. Coins in a liquidation cascade in the last 3h: {breadth}.

ALERTS - pattern, side, coin, then: change 4h %, change 24h %, change 7d %, strength vs BTC 24h %, open interest 4h % / 24h %,
24h volume vs normal, aggressive-buy share 24h, funding bp, retail long share rank vs its last 30 days (0 = lowest, 1 = highest):
{table}

Every alert already meets its pattern's trigger. The side of each alert is fixed by its pattern (long = buy the coin, short = sell it).
Which alerts do you trade now (at most {free})? Answer with JSON only, the reason in Russian, at most 25 words:
{{"trade": ["COIN", ...], "reason": "одно короткое предложение"}}"""

RUNNER = r'''import json, sys, urllib.request
src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh, open(dst + ".part", "w", encoding="utf-8") as out:
    for line in fh:
        job = json.loads(line)
        try:
            req = urllib.request.Request("http://127.0.0.1:8788/completion", data=json.dumps(job).encode(),
                                         headers={"Content-Type": "application/json"})
            content = json.loads(urllib.request.urlopen(req, timeout=1800).read()).get("content", "")
        except Exception as exc:
            content = "ERROR " + str(exc)
        out.write(json.dumps({"content": content}, ensure_ascii=False) + "\n")
        out.flush()
import os
os.replace(dst + ".part", dst)
'''


def prepare(extra=True):
    """Базовые 20 пар (flow_cache) и, с v3, пары EXTRA (flow_cache_extra); широта — по базовым."""
    data = dict(L.load_all())
    cascade = PATTERNS['P1']['conditions'][:2]
    trig = pd.DataFrame({p: pd.Series(L.signal_mask(f, cascade), index=f.index) for p, (d, f) in data.items()}).fillna(False)
    breadth = trig.rolling(3, min_periods=1).max().sum(axis=1)
    folder = os.path.join(HERE, 'flow_cache_extra')
    if extra and os.path.isdir(folder):
        btc = data['BTCUSDT'][0]
        for p in EXTRA:
            path = os.path.join(folder, f'{p}.pkl')
            if os.path.exists(path):
                df = pd.read_pickle(path)
                df = df[df['v'] > 0]
                data[p] = (df, L.features(df, btc))
    for p, (d, f) in data.items():
        f['cascade_count_3h'] = breadth.reindex(f.index).to_numpy()
    return data


def alerts(data, split):
    """{час: [(монета, закономерность), …]} в периоде, в порядке механики."""
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
    by_hour = {}
    for key, pat in PATTERNS.items():
        for p, (df, f) in data.items():
            if pat.get('universe') == 'core' and p in EXTRA:
                continue
            m = L.signal_mask(f, pat['conditions'])
            # Решение — один раз на серию: только первый час, когда сигнал появился
            # (вживую модель спрашивается о новом сигнале, а не каждый час, пока он держится).
            start = m & ~np.concatenate([[False], m[:-1]])
            for t in f.index[start]:
                if a <= t < b:
                    by_hour.setdefault(t, []).append((p, key))
    out = []
    for t in sorted(by_hour):
        items = sorted(by_hour[t], key=lambda x: (list(PATTERNS).index(x[1]),
                                                   PATTERNS[x[1]]['rank'][1] * data[x[0]][1].at[t, PATTERNS[x[1]]['rank'][0]]))
        seen, uniq = set(), []
        for p, key in items:
            if p not in seen:
                seen.add(p)
                uniq.append((p, key))
        out.append((t, uniq))
    return out


def outcomes(data, evs):
    res = {}
    for t, items in evs:
        for p, key in items:
            df, f = data[p]
            i = f.index.get_loc(t)
            mask = np.zeros(len(f), bool)
            mask[i] = True
            pat = PATTERNS[key]
            tr = L.simulate(df, mask, pat['side'], pat['hold'], pat['stop'], f['atr_d'].to_numpy(float))
            if tr:
                res[(t, p)] = (tr[0][2], t + pd.Timedelta(hours=tr[0][3] + 1), key)
    return res


def play(evs, res, choose):
    """choose(t, items, held, free) -> монеты; места и занятость — общие для всех политик."""
    held, got = {}, []
    for t, items in evs:
        held = {p: e for p, e in held.items() if e > t}
        free = SLOTS - len(held)
        cand = [(p, k) for p, k in items if p not in held and (t, p) in res]
        if free <= 0 or not cand:
            continue
        names = {p for p, _ in cand}
        for p in choose(t, cand, list(held), free)[:free]:
            if p in names and p not in held:
                r, exit_t, key = res[(t, p)]
                got.append((r, key))
                held[p] = exit_t
    return got


def brief(data, t, items, held, free):
    btc = data['BTCUSDT'][1].loc[t]
    rows = []
    for p, key in items:
        f = data[p][1].loc[t]
        rows.append(f"{key} {PATTERNS[key]['side']:5s} {p.replace('USDT', ''):9s} {f['ret_4h']:+6.1f} {f['ret_24h']:+6.1f} {f['ret_7d']:+6.1f} "
                    f"{f['rel_24h']:+6.1f} {f['oi_chg_4h']:+5.1f}/{f['oi_chg_24h']:+5.1f} {f['vol_z']:5.2f} "
                    f"{f['taker_24h']:5.3f} {f['funding_bp']:+5.2f} {f['buy_ratio_pct_30d']:.2f}")
    return QUESTION.format(time=(t + pd.Timedelta(hours=1)).strftime('%Y-%m-%d %H:%M'), n=len(items),
                           held=', '.join(p.replace('USDT', '') for p in held) or 'nothing', free=free,
                           btc4=btc['btc_ret_4h'], btc24=btc['btc_ret_24h'], btc7=btc['btc_ret_7d'], btc30=btc['btc_ret_30d'],
                           breadth=int(btc['cascade_count_3h']), table='\n'.join(rows))


def ask_model(prompts, tag):
    """Вопросы пачкой на сервер (без мысли); ответ на каждый вопрос строкой, и при ошибке тоже."""
    os.makedirs(OUT, exist_ok=True)
    local = os.path.join(OUT, f'{tag}_jobs.jsonl')
    with open(local, 'w', encoding='utf-8') as fh:
        for text in prompts:
            raw = (f'<|im_start|>system\n{NOTEBOOK}<|im_end|>\n<|im_start|>user\n{text}<|im_end|>\n'
                   f'<|im_start|>assistant\n<think>\n\n</think>\n\n')
            fh.write(json.dumps({'prompt': raw, 'n_predict': 160, 'temperature': 0.2, 'top_k': 20, 'top_p': 0.9,
                                 'stop': ['<|im_end|>'], 'cache_prompt': True}, ensure_ascii=False) + '\n')
    runner = os.path.join(OUT, 'run_jobs.py')
    open(runner, 'w', encoding='utf-8', newline='\n').write(RUNNER)
    subprocess.run(R.SSH + [f'mkdir -p {R.REMOTE}'], check=True)
    subprocess.run(R.SCP + [local, runner, f'root@195.133.35.5:{R.REMOTE}/'], check=True)
    subprocess.run(R.SSH + [f'cd {R.REMOTE} && rm -f {tag}_answers.jsonl && nohup python3 run_jobs.py '
                            f'{tag}_jobs.jsonl {tag}_answers.jsonl > {tag}_runner.log 2>&1 &'], check=True)
    return collect(tag, len(prompts))


def collect(tag, n):
    started = time.time()
    while True:
        time.sleep(60)
        r = subprocess.run(R.SSH + [f'test -s {R.REMOTE}/{tag}_answers.jsonl && cat {R.REMOTE}/{tag}_answers.jsonl'],
                           capture_output=True, text=True, encoding='utf-8')
        if r.stdout.strip():
            lines = [json.loads(x)['content'] for x in r.stdout.strip().splitlines()]
            open(os.path.join(OUT, f'{tag}_answers.jsonl'), 'w', encoding='utf-8').write(r.stdout)
            print(f'ответы: {len(lines)} из {n} за {(time.time() - started) / 60:.0f} мин', flush=True)
            return lines
        if time.time() - started > 12 * 3600:
            raise TimeoutError('модель не ответила за 12 ч')


def picks_from(text, items):
    try:
        body = json.loads(text[text.index('{'):text.rindex('}') + 1])
        buy = body.get('trade', body.get('buy')) or []          # v3.2 — "trade", до неё — "buy"
    except Exception:                                  # noqa: BLE001
        return None
    names = {p.replace('USDT', ''): p for p, _ in items}
    return [names[str(x).upper().replace('USDT', '')] for x in buy if str(x).upper().replace('USDT', '') in names]


def summary(name, got, weeks):
    r = np.array([x[0] for x in got])
    if not len(r):
        print(f'{name:10s}    0 сд', flush=True)
        return
    t_stat = r.mean() / r.std(ddof=1) * np.sqrt(len(r)) if len(r) > 1 else float('nan')
    by = {k: np.array([x[0] for x in got if x[1] == k]) for k in PATTERNS}
    parts = ', '.join(f"{k} {len(v)} сд {v.mean():+.3f}R" for k, v in by.items() if len(v))
    eq = np.cumsum(r)
    dd = float(np.min(eq - np.maximum.accumulate(eq)))
    print(f'{name:10s} {len(r):4d} сд {len(r) / weeks:4.1f}/нед R {r.mean():+.3f} t {t_stat:+.1f} '
          f'сумма {r.sum():+6.1f}R просадка {dd:.1f}R плюс {np.mean(r > 0) * 100:3.0f}%  ({parts})', flush=True)


def run(split, limit=None):
    data = prepare()
    evs = alerts(data, split)
    if limit:
        evs = evs[:limit]
    res = outcomes(data, evs)
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS[split])
    if limit:
        b = min(b, evs[-1][0] + pd.Timedelta(days=1))
    weeks = (b - a).days / 7
    print(f'{split}: срезов {len(evs)}, возможных сделок {len(res)} '
          f"(P1 {sum(1 for v in res.values() if v[2] == 'P1')}, P2 {sum(1 for v in res.values() if v[2] == 'P2')})",
          flush=True)
    mech = play(evs, res, lambda t, c, h, f: [p for p, _ in c])
    rng = np.random.default_rng(7)
    rand = [np.mean([x[0] for x in play(evs, res, lambda t, c, h, f: [p for p, _ in rng.permutation(c)])])
            for _ in range(300)]
    # Вопрос модели строится для каждого среза заранее; «держит» и «свободно» в вопросе — по механике
    # (её ответы на прошлые срезы ещё неизвестны); исполнение — с общими правилами мест и занятости.
    held, prompts = {}, []
    for t, items in evs:
        held = {p: e for p, e in held.items() if e > t}
        free = max(1, SLOTS - len(held))
        cand = [(p, k) for p, k in items if p not in held and (t, p) in res] or items
        prompts.append(brief(data, t, cand, list(held), free))
        for p, _ in cand[:max(0, SLOTS - len(held))]:
            if (t, p) in res:
                held[p] = res[(t, p)][1]
    tag = f'{VERSION}_{split}{limit or ""}'
    answers = ask_model(prompts, tag)
    chosen = {t: picks_from(text, items) for (t, items), text in zip(evs, answers)}
    bad = sum(1 for v in chosen.values() if v is None)
    skipped = sum(1 for v in chosen.values() if v == [])
    model = play(evs, res, lambda t, c, h, f: chosen.get(t) or [])
    print(f'модель: ответов без JSON {bad}, срезов «ничего не брать» {skipped} из {len(evs)}', flush=True)
    summary('модель', model, weeks)
    summary('механика', mech, weeks)
    print(f'{"случайно":10s} средний R {np.mean(rand):+.3f} (5–95%: {np.percentile(rand, 5):+.3f} … '
          f'{np.percentile(rand, 95):+.3f})', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    run(sys.argv[1] if len(sys.argv) > 1 else 'valid', int(sys.argv[2]) if len(sys.argv) > 2 else None)
