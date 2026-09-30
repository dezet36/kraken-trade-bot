"""
Тетрадь v3.4 против v3.3 на истории (30.09.2026, docs/ИИ_замечания_на_проверку.md, п. 77).

v3.4 = v3.3 без раздела P2 (выключена владельцем, п. 75) и с разделом P4
«сжатие шортистов» (правило круга 1, прошло ступень А круга 5, п. 76): фандинг
≤ −2 bp и доля розничных лонгов в нижних 3% месяца — лонг 48 ч, стоп 1.5
размаха; на всех 30 парах. Вопрос — v3.3 без изменений. Условия приёмки — п. 77.

Наборы: S_P4 — до 40 срезов с сигналом P4 (seed 17); S_C и S_B3 — те же 40 + 40
срезов, что в п. 72 (ai_question_v33), без сигналов P2; основа — ответы v3.3.

    python research/ai_question_v34.py dry       # вопрос-образец, без модели
    python research/ai_question_v34.py run       # задания на сервер, ответы, итог
    python research/ai_question_v34.py collect   # ответы уже на сервере — забрать и посчитать
"""
import json
import os
import re
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_model_research as R                         # noqa: E402
import ai_model_trader_bt as B                        # noqa: E402
import ai_question_v31 as Q                           # noqa: E402
import ai_question_v33 as W                           # noqa: E402

TAG = 'v34_check'
SAMPLE = 40
RESULT = os.path.join(HERE, 'results', 'ai_question_v34.txt')

P4 = {'label': 'SHORT SQUEEZE', 'side': 'long', 'hold': 48, 'stop': 1.5, 'rank': ('funding_bp', 1),
      'conditions': [['funding_bp', '<=', -2.0], ['buy_ratio_pct_30d', '<=', 0.03]]}

P4_TEXT = """P4. SHORT SQUEEZE (long, hold 48h, stop = 1.5 x the coin's average daily range)
   Trigger: funding is deeply negative (-2 bp per 8h or lower: shorts pay longs every 8 hours) and the share of retail
   accounts in longs is at the lowest 3% of its last 30 days - the crowd is maximally short and pays to stay short.
   Buy the coin: the shorts give up or get squeezed.
   Result: +0.113R per trade over 386 trades, 50% winners (winners are bigger than losers).

"""


def _notebook_v34():
    text = W.NOTEBOOK_V33
    a, b = text.index('P2. BTC DIVERGENCE ACCUMULATION'), text.index('B3. DISTRIBUTION ON MARKET BOUNCE')
    text = text[:a] + text[b:]
    r = text.index('RISK: at most 6 open positions.')
    return text[:r] + P4_TEXT + text[r:]


NOTEBOOK_V34 = _notebook_v34()
QUESTION_V34 = W.QUESTION_V33
assert 'P2.' not in NOTEBOOK_V34 and 'P4. SHORT SQUEEZE' in NOTEBOOK_V34


def plan():
    # Срезы п. 72 — при прежних закономерностях (P1, P2, B3), до подмены.
    jobs33, _prompts33, res33 = W.plan()
    s_c = [s for kind, _i, s in jobs33 if kind == 'c']
    s_b3 = [s for kind, _i, s in jobs33 if kind == 'b3']
    # Тетрадь v3.4: P1, B3, P4.
    B.PATTERNS = {'P1': B.PATTERNS['P1'], 'B3': B.PATTERNS['B3'], 'P4': P4}
    data = B.prepare()
    all34, res34 = [], {}
    for split in ('train', 'valid'):
        s, r = Q.slices(data, split)
        all34 += s
        res34.update(r)
    with_p4 = [s for s in all34 if any(k == 'P4' for _, k in s['items'])]
    rng = np.random.default_rng(17)
    s_p4 = [with_p4[i] for i in sorted(rng.choice(len(with_p4), size=min(SAMPLE, len(with_p4)), replace=False))]

    def no_p2(s):
        return dict(s, items=[(p, k) for p, k in s['items'] if k != 'P2'])

    jobs = ([('p4', None, s) for s in s_p4]
            + [('c', i, no_p2(s)) for i, s in enumerate(s_c) if no_p2(s)['items']]
            + [('b3', 40 + i, no_p2(s)) for i, s in enumerate(s_b3) if no_p2(s)['items']])
    prompts = [Q.brief_v31(data, s['t'], s['items'], s['held'], s['free'], template=QUESTION_V34)
               for _, _, s in jobs]
    res = {**res33, **res34}
    print(f'срезов с P4 {len(with_p4)} (выборка {len(s_p4)}); вопросов модели {len(prompts)}', flush=True)
    return jobs, prompts, res, jobs33


def raw(question):
    return {'prompt': (f'<|im_start|>system\n{NOTEBOOK_V34}<|im_end|>\n<|im_start|>user\n{question}<|im_end|>\n'
                       f'<|im_start|>assistant\n<think>\n\n</think>\n\n'),
            'n_predict': 160, 'temperature': 0.2, 'top_k': 20, 'top_p': 0.9, 'stop': ['<|im_end|>'],
            'cache_prompt': True}


def ask(prompts):
    os.makedirs(B.OUT, exist_ok=True)
    local = os.path.join(B.OUT, f'{TAG}_jobs.jsonl')
    with open(local, 'w', encoding='utf-8', newline='\n') as fh:
        for q in prompts:
            fh.write(json.dumps(raw(q), ensure_ascii=False) + '\n')
    runner = os.path.join(B.OUT, 'run_jobs.py')
    open(runner, 'w', encoding='utf-8', newline='\n').write(B.RUNNER)
    subprocess.run(R.SCP + [local, runner, f'root@195.133.35.5:{R.REMOTE}/'], check=True)
    subprocess.run(W.SSH_N + [f'cd {R.REMOTE} && rm -f {TAG}_answers.jsonl && nohup python3 run_jobs.py '
                              f'{TAG}_jobs.jsonl {TAG}_answers.jsonl > {TAG}_runner.log 2>&1 &'], check=True)
    return B.collect(TAG, len(prompts))


def evaluate(jobs, answers, res, jobs33):
    import pandas as pd
    lines = []

    def say(text=''):
        print(text, flush=True)
        lines.append(text)

    base = [json.loads(x)['content'] for x in open(os.path.join(B.OUT, 'v33_check_answers.jsonl'), encoding='utf-8')]
    rows, bad, reasons, russian = [], 0, 0, 0
    for (kind, idx, s), text in zip(jobs, answers):
        chosen = Q.picks(text, s['items'])
        bad += chosen is None
        reason = W.reason_of(text)
        if reason:
            reasons += 1
            russian += bool(re.search('[а-яА-ЯёЁ]', reason))
        chosen = (chosen or [])[:s['free']]
        for p, k in s['items']:
            rows.append({'set': kind, 'v': 'v3.4', 'key': k, 'taken': p in chosen, 'r': res[(s['t'], p)][0],
                         'room': min(sum(1 for _, kk in s['items'] if kk == k), s['free'])})
        if idx is not None:                           # та же пара срезов у v3.3: её выбор по P1/B3
            _k, _i, s33 = jobs33[idx]
            old = (Q.picks(base[idx], s33['items']) or [])[:s33['free']]
            for p, k in s33['items']:
                if k in ('P1', 'B3'):
                    rows.append({'set': kind, 'v': 'v3.3', 'key': k, 'taken': p in old, 'r': res[(s33['t'], p)][0],
                                 'room': None})
    d = pd.DataFrame(rows)
    say('Тетрадь v3.4 против v3.3 на train + valid (п. 77)')
    for (kind, version, key), g in d.groupby(['set', 'v', 'key']):
        taken = g[g.taken]
        say(f"  {({'p4': 'S_P4', 'c': 'S_C ', 'b3': 'S_B3'})[kind]} {version} {key}: взято {len(taken)}/{len(g)}, "
            f"R взятых {taken.r.mean() if len(taken) else float('nan'):+.3f} (всех {g.r.mean():+.3f})")

    p4 = d[(d.set == 'p4') & (d.key == 'P4')]
    rooms = sum(min(sum(1 for _, k in s['items'] if k == 'P4'), s['free']) for kind, _i, s in jobs if kind == 'p4')
    c1 = int(p4.taken.sum()) / rooms if rooms else float('nan')

    def rate(kind, version, key):
        g = d[(d.set == kind) & (d.v == version) & (d.key == key)]
        return g.taken.mean() if len(g) else float('nan')

    c2 = abs(rate('c', 'v3.4', 'P1') - rate('c', 'v3.3', 'P1'))
    c3 = abs(rate('b3', 'v3.4', 'B3') - rate('b3', 'v3.3', 'B3'))
    c4 = bad / len(answers)
    c5 = russian / reasons if reasons else float('nan')
    checks = [(f'1. S_P4: берёт P4 {c1 * 100:.0f}% возможного (нужно ≥ 50%)', c1 >= 0.50),
              (f'2. S_C: доля P1 v3.4 − v3.3 {c2 * 100:.0f} п. п. (нужно ≤ 15)', c2 <= 0.15),
              (f'3. S_B3: доля B3 v3.4 − v3.3 {c3 * 100:.0f} п. п. (нужно ≤ 15)', c3 <= 0.15),
              (f'4. без целого JSON {c4 * 100:.1f}% (нужно ≤ 5%)', c4 <= 0.05),
              (f'5. причина по-русски {c5 * 100:.0f}% (нужно ≥ 90%)', c5 >= 0.90)]
    say()
    for text, ok in checks:
        say(f"  {'да ' if ok else 'НЕТ'} {text}")
    verdict = all(ok for _, ok in checks)
    say(f"\nИтог: v3.4 {'принят — идёт вживую' if verdict else 'не принят — остаётся v3.3'}")
    say('\nПримеры причин по P4:')
    for (kind, _i, _s), text in zip(jobs, answers):
        if kind == 'p4':
            say(f'  {W.reason_of(text)}')
            if sum(1 for x in lines if x.startswith('  ')) > 30:
                break
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return verdict


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    mode = sys.argv[1] if len(sys.argv) > 1 else 'dry'
    jobs, prompts, res, jobs33 = plan()
    if mode == 'dry':
        first = next(i for i, (k, _i, _s) in enumerate(jobs) if k == 'p4')
        print(NOTEBOOK_V34[-1400:])
        print(prompts[first])
    else:
        answers = B.collect(TAG, len(prompts)) if mode == 'collect' else ask(prompts)
        evaluate(jobs, answers, res, jobs33)
