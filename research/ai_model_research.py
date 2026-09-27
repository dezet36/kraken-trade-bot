"""
Модель ищет закономерности сама (27.09.2026): предлагает правила, стенд
проверяет их на train и возвращает ей цифры, она правит — несколько кругов.

Модель видит только train (2022-01…2024-06): признаки, их распределение и
итоги своих правил на нём. valid и test ей не показываются — по ним правила
принимаются и проверяются (research/ai_pattern_lab.py).

Модель — боевая (llama-server на сервере, Qwen3.6-35B-A3B), бот в режиме
«правила» её не занимает. Запрос уходит на сервер и ждёт там (nohup curl),
ответ забирается, когда готов.

    python research/ai_model_research.py round 1          # первый круг
    python research/ai_model_research.py round 2          # правка по итогам круга 1
    python research/ai_model_research.py score            # итог всех правил модели: train/valid
"""
import json
import os
import re
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_pattern_lab as L                            # noqa: E402

OUT = os.path.join(HERE, 'results', 'model_research')
SSH = ['ssh', '-i', 'C:/Users/Лаки/.ssh/kraken', '-o', 'UserKnownHostsFile=C:/Users/Лаки/.ssh/known_hosts',
       '-o', 'IdentitiesOnly=yes', '-o', 'ConnectTimeout=20', '-p', '53547', 'root@195.133.35.5']
SCP = ['scp', '-i', 'C:/Users/Лаки/.ssh/kraken', '-o', 'UserKnownHostsFile=C:/Users/Лаки/.ssh/known_hosts',
       '-o', 'IdentitiesOnly=yes', '-P', '53547']
REMOTE = '/opt/kraken/llm_exp/patterns'

DESCR_EN = {
    'ret_1h': 'price change over the last 1h, %', 'ret_4h': 'price change over 4h, %',
    'ret_24h': 'price change over 24h, %', 'ret_72h': 'price change over 72h, %', 'ret_7d': 'price change over 7 days, %',
    'range_pos_7d': 'position of price in its 7-day high-low range: 0 = at the low, 1 = at the high',
    'atr_d': 'average daily high-low range over 14 days, % of price',
    'vol_z': 'quote volume of the last 24h divided by the 30-day median daily volume (1 = normal)',
    'taker_1h': 'share of volume bought aggressively (market buys) in the last hour; 0.5 = balanced',
    'taker_4h': 'aggressive-buy share over 4h', 'taker_24h': 'aggressive-buy share over 24h',
    'taker_24h_dev': 'taker_24h minus its own 30-day average (positive = unusual buying pressure)',
    'size_z': 'average trade size over 24h divided by its 30-day median (>1 = bigger players active)',
    'buy_ratio': 'share of Bybit accounts that are long (retail crowd positioning), 0..1',
    'buy_ratio_chg_24h': 'change of buy_ratio over 24h, percentage points',
    'buy_ratio_pct_30d': 'percentile of buy_ratio within the last 30 days, 0..1',
    'oi_chg_4h': 'open interest change over 4h, %', 'oi_chg_24h': 'open interest change over 24h, %',
    'oi_chg_72h': 'open interest change over 72h, %',
    'funding_bp': 'last funding rate, basis points per 8h (positive = longs pay shorts)',
    'funding_pct_30d': 'percentile of funding within the last 30 days, 0..1',
    'btc_ret_4h': 'BTC change over 4h, %', 'btc_ret_24h': 'BTC change over 24h, %', 'btc_ret_7d': 'BTC change over 7 days, %',
    'rel_24h': 'this coin 24h change minus BTC 24h change, %',
    'hour_utc': 'UTC hour of the decision (0..23)', 'weekday': 'weekday of the decision (0 = Monday)',
}

TASK = """You are a quantitative researcher. Your job: discover trading rules for crypto perpetual futures that make money AFTER costs.

MARKET AND EXECUTION
- 20 liquid USDT perpetuals on Bybit (BTC, ETH, SOL, XRP, BNB, DOGE, ADA, AVAX, LINK, LTC, AAVE, ARB, COTI, DOT, NEAR, SHIB, SUI, UNI, XLM, ZEC).
- A rule is checked at the close of every hour on every coin. When all its conditions are true, the bot enters at the next hour's open with a market order.
- Exit: stop-loss at stop_atr x atr_d from entry, otherwise at the close after hold_hours. One open position per rule per coin.
- Round-trip cost is about 0.17% of price (fees + slippage). A trade with a 5% stop pays ~0.03R in costs.
- Target frequency: all your rules together should trade about 2-5 times per week across the 20 coins, i.e. each rule roughly 0.02-0.3 signals per coin per week. Very rare rules are useless; very frequent rules drown in costs.

DATA (hourly, 2022-01..2024-06 shown to you; later years are held out to test you)
{features}

RULE FORMAT (JSON only)
{{"rules": [
  {{"name": "short snake_case name", "side": "long" or "short",
    "conditions": [["feature", "<" | "<=" | ">" | ">=", number], ...],
    "hold_hours": integer 6..96, "stop_atr": number 0.3..2.0,
    "why": "one sentence: the market mechanism you exploit"}}
]}}
Use only the feature names above. 2-4 conditions per rule. Think about WHO is forced to trade and WHY price should move: crowd positioning, liquidations, aggressive flow, funding costs, reversal vs continuation.
{history}
Propose {n} rules. Answer with the JSON object only."""


def distribution_text():
    data = L.load_all()
    a, b = (np.datetime64(x) for x in L.SPLITS['train'])
    frames = []
    for pair, (df, feat) in data.items():
        idx = feat.index.tz_convert(None).to_numpy()
        frames.append(feat[(idx >= a) & (idx < b)])
    import pandas as pd
    allf = pd.concat(frames)
    lines = []
    for name, descr in DESCR_EN.items():
        q = allf[name].quantile([0.05, 0.25, 0.5, 0.75, 0.95]).to_numpy()
        lines.append(f'- {name}: {descr}. Typical values (5/25/50/75/95%): '
                     + ' / '.join(f'{v:.3g}' for v in q))
    return '\n'.join(lines)


def history_text(upto_round):
    """Итоги прежних кругов — только train."""
    rows = []
    for k in range(1, upto_round):
        path = os.path.join(OUT, f'round{k}_scored.json')
        if not os.path.exists(path):
            continue
        for item in json.load(open(path, encoding='utf-8')):
            tr = item['train']
            head = (f"- round {k} {item['rule']['name']} ({item['rule']['side']}, "
                    f"{json.dumps(item['rule']['conditions'])}, hold {item['rule'].get('hold_hours')}, "
                    f"stop {item['rule'].get('stop_atr')}): ")
            if not tr['n'] or tr['r'] is None or tr['t'] is None:
                rows.append(head + f"{tr['n']} trades - too rare, conditions never met together")
                continue
            rows.append(head + f"{tr['n']} trades, {tr['per_week']:.2f}/week, "
                        f"avg {tr['r']:+.3f}R per trade, t={tr['t']:+.1f}, win {tr['win'] * 100:.0f}%")
    if not rows:
        return ''
    return ('\nRESULTS OF YOUR PREVIOUS RULES ON 2022-01..2024-06 (after costs; t>=2 and >=1 trade/week '
            'across all coins is what counts; negative average means the idea loses):\n' + '\n'.join(rows)
            + diagnostics_text(upto_round)
            + '\nKeep what works, fix what almost works, drop what loses, and try new mechanisms.\n')


DIAG_FEATURES = ('ret_24h', 'ret_7d', 'btc_ret_24h', 'btc_ret_7d', 'buy_ratio_pct_30d', 'funding_bp',
                 'taker_24h', 'vol_z', 'size_z', 'oi_chg_24h', 'range_pos_7d')


def diagnostics_text(upto_round, top=3):
    """Разбор лучших правил: средний R по третям признаков среди их сделок — только train."""
    import pandas as pd
    items = []
    for k in range(1, upto_round):
        path = os.path.join(OUT, f'round{k}_scored.json')
        if os.path.exists(path):
            items += json.load(open(path, encoding='utf-8'))
    items = [x for x in items if x['train']['n'] and x['train']['n'] >= 100 and x['train']['t'] is not None]
    items.sort(key=lambda x: -x['train']['t'])
    if not items:
        return ''
    data = L.load_all()
    a, b = (pd.Timestamp(x, tz='UTC') for x in L.SPLITS['train'])
    out = ['\nDIAGNOSTICS OF YOUR BEST RULES ON 2022-01..2024-06: average R of their trades split into thirds of each feature '
           '(low / middle / high third, with the feature range). Use it to add or tighten conditions.']
    for item in items[:top]:
        rule = item['rule']
        _, trades = L.evaluate(rule, data=data, splits=('train',))
        trades = trades[(trades['t'] >= a) & (trades['t'] < b)].copy()
        for name in DIAG_FEATURES:
            trades[name] = [data[p][1].at[t, name] for p, t in zip(trades['pair'], trades['t'])]
        out.append(f"- {rule['name']} ({len(trades)} trades, avg {trades['r'].mean():+.3f}R):")
        for name in DIAG_FEATURES:
            col = trades[name]
            if col.nunique() < 3:
                continue
            q = col.quantile([1 / 3, 2 / 3]).to_numpy()
            parts = []
            for lo, hi, label in ((-np.inf, q[0], 'low'), (q[0], q[1], 'mid'), (q[1], np.inf, 'high')):
                sel = trades[(col > lo) & (col <= hi)]
                if len(sel):
                    parts.append(f"{label} {sel['r'].mean():+.3f}R ({len(sel)})")
            out.append(f"    {name}: " + ', '.join(parts) + f" (thirds split at {q[0]:.3g} and {q[1]:.3g})")
    return '\n'.join(out) + '\n'


def ask(prompt, tag, max_tokens=6000):
    os.makedirs(OUT, exist_ok=True)
    payload = {'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': max_tokens,
               'temperature': 0.6, 'top_p': 0.95, 'top_k': 20}
    local = os.path.join(OUT, f'{tag}_request.json')
    json.dump(payload, open(local, 'w', encoding='utf-8'), ensure_ascii=False)
    subprocess.run(SSH + [f'mkdir -p {REMOTE}'], check=True)
    subprocess.run(SCP + [local, f'root@195.133.35.5:{REMOTE}/{tag}_request.json'], check=True)
    subprocess.run(SSH + [f'cd {REMOTE} && rm -f {tag}_answer.json && nohup curl -s -m 7200 '
                          f'-H "Content-Type: application/json" -d @{tag}_request.json '
                          f'http://127.0.0.1:8788/v1/chat/completions > {tag}_answer.json.part 2>&1 && '
                          f'mv {tag}_answer.json.part {tag}_answer.json &'], check=True)
    started = time.time()
    while True:
        time.sleep(60)
        r = subprocess.run(SSH + [f'test -s {REMOTE}/{tag}_answer.json && cat {REMOTE}/{tag}_answer.json'],
                           capture_output=True, text=True, encoding='utf-8')
        if r.stdout.strip():
            answer = json.loads(r.stdout)
            json.dump(answer, open(os.path.join(OUT, f'{tag}_answer.json'), 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            print(f'ответ за {(time.time() - started) / 60:.0f} мин', flush=True)
            return answer['choices'][0]['message']['content']
        if time.time() - started > 7400:
            raise TimeoutError('модель не ответила за 2 ч')


def finish(tag, n_predict=2500):
    """
    Мысль модели съела весь предел — ответа нет. Доводим: та же подсказка, её
    же мысль, закрытый </think> — модель пишет только JSON. Формат — родной
    шаблон Qwen (ChatML), как у бота.
    """
    answer = json.load(open(os.path.join(OUT, f'{tag}_answer.json'), encoding='utf-8'))
    request = json.load(open(os.path.join(OUT, f'{tag}_request.json'), encoding='utf-8'))
    thought = answer['choices'][0]['message'].get('reasoning_content') or ''
    prompt = request['messages'][0]['content']
    # Контекст 12288: вопрос + мысль + ответ. Не влезает — берём хвост мысли:
    # там модель сводит и проверяет правила перед выдачей.
    # Числа и JSON токенизируются плотнее текста: ~2.4 знака на токен (замер 27.09).
    room_chars = int((12288 - n_predict - 300) * 2.4) - len(prompt)
    if len(thought) > room_chars:
        thought = '...\n' + thought[-max(2000, room_chars):]
    raw = (f'<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n<think>\n{thought.strip()}\n'
           f'I have enough. Now I output the final JSON with all rules.\n</think>\n\n')
    payload = {'prompt': raw, 'n_predict': n_predict, 'temperature': 0.3, 'top_p': 0.95, 'top_k': 20,
               'stop': ['<|im_end|>'], 'cache_prompt': True}
    local = os.path.join(OUT, f'{tag}_finish_request.json')
    json.dump(payload, open(local, 'w', encoding='utf-8'), ensure_ascii=False)
    subprocess.run(SCP + [local, f'root@195.133.35.5:{REMOTE}/{tag}_finish_request.json'], check=True)
    subprocess.run(SSH + [f'cd {REMOTE} && rm -f {tag}_finish.json && nohup curl -s -m 7200 '
                          f'-H "Content-Type: application/json" -d @{tag}_finish_request.json '
                          f'http://127.0.0.1:8788/completion > {tag}_finish.json.part 2>&1 && '
                          f'mv {tag}_finish.json.part {tag}_finish.json &'], check=True)
    started = time.time()
    while True:
        time.sleep(60)
        r = subprocess.run(SSH + [f'test -s {REMOTE}/{tag}_finish.json && cat {REMOTE}/{tag}_finish.json'],
                           capture_output=True, text=True, encoding='utf-8')
        if r.stdout.strip():
            out = json.loads(r.stdout)
            json.dump(out, open(os.path.join(OUT, f'{tag}_finish.json'), 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            print(f'ответ за {(time.time() - started) / 60:.0f} мин', flush=True)
            return out['content']
        if time.time() - started > 7400:
            raise TimeoutError('модель не ответила за 2 ч')


def parse_rules(text):
    body = text.split('</think>')[-1]
    m = re.search(r'\{.*\}', body, re.S)
    try:
        rules = json.loads(m.group(0))['rules']
    except Exception:                                  # noqa: BLE001
        # Оборванный или с огрехом JSON: вынимаем каждое целое правило отдельно.
        rules = []
        for chunk in re.findall(r'\{[^{}]*"name"[^{}]*"conditions"\s*:\s*\[.*?\]\s*\][^{}]*\}', body, re.S):
            try:
                rules.append(json.loads(chunk))
            except Exception:                          # noqa: BLE001
                continue
    ok = []
    for r in rules:
        conds = [c for c in r.get('conditions', []) if len(c) == 3 and c[0] in L.FEATURES and c[1] in L.OPS]
        if not conds or r.get('side') not in ('long', 'short'):
            continue
        r['conditions'] = conds
        r['hold_hours'] = int(min(96, max(6, int(r.get('hold_hours', 24)))))
        r['stop_atr'] = float(min(2.0, max(0.3, float(r.get('stop_atr', 1.0)))))
        ok.append(r)
    return ok


def score(rules, tag):
    out = []
    for r in rules:
        summary, _ = L.evaluate(r, splits=('train', 'valid', 'test'))
        clean = {s: {k: (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in summary[s].items()}
                 for s in summary}
        out.append({'rule': r, **clean})
        print(L.line(r['name'], summary), flush=True)
    # В файл для модели — только train (history_text читает только его).
    json.dump(out, open(os.path.join(OUT, f'{tag}_scored.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'round'
    os.makedirs(OUT, exist_ok=True)
    if cmd == 'round':
        k = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        prompt = TASK.format(features=distribution_text(), history=history_text(k), n=10)
        open(os.path.join(OUT, f'round{k}_prompt.txt'), 'w', encoding='utf-8').write(prompt)
        # Контекст сервера 12288 токенов: вопрос (~3.2 знака на токен) + мысль и ответ.
        budget = int(min(9000, 11800 - len(prompt) / 3.2))
        text = ask(prompt, f'round{k}', max_tokens=max(3000, budget))
        if '{' not in text.split('</think>')[-1]:
            text = finish(f'round{k}')
        rules = parse_rules(text)
        print(f'круг {k}: правил {len(rules)}', flush=True)
        score(rules, f'round{k}')
    elif cmd == 'finish':
        k = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        rules = parse_rules(finish(f'round{k}'))
        print(f'круг {k}: правил {len(rules)}', flush=True)
        score(rules, f'round{k}')
    elif cmd == 'collect':
        # Запрос уже на сервере (связь оборвалась при запуске) — ждём файл и оцениваем.
        k = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        tag = f'round{k}'
        while True:
            r = subprocess.run(SSH + [f'test -s {REMOTE}/{tag}_finish.json && cat {REMOTE}/{tag}_finish.json'],
                               capture_output=True, text=True, encoding='utf-8')
            if r.stdout.strip():
                out = json.loads(r.stdout)
                json.dump(out, open(os.path.join(OUT, f'{tag}_finish.json'), 'w', encoding='utf-8'),
                          ensure_ascii=False, indent=1)
                break
            time.sleep(60)
        rules = parse_rules(out['content'])
        print(f'круг {k}: правил {len(rules)}', flush=True)
        score(rules, tag)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
