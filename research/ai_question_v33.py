"""
Тетрадь и вопрос v3.3 против v3.2 на истории (28.09.2026, docs/ИИ_замечания_на_проверку.md, п. 72).

v3.2 берёт треть сигналов P1 из каскада — тетрадь велит «одна ставка, бери
сильнейшие»; на истории это стоит ~1 сделку в неделю и 15–30% суммы
(research/ai_p1_cluster.py). v3.3 = v3.2 + строка RISK «бери все подходящие
сигналы, каскад даёт несколько хороших сделок сразу» + причина по-русски.
Условия приёмки — п. 72, записаны до прогона; test не трогаем.

Наборы (seed 13): S_C — 40 срезов каскада (≥ 3 сигналов P1), S_B3 — 40 из 83
срезов с B3, S_P — те же 40, что в п. 70 (основа — ответы v3.2 из прогона п. 70).

    python research/ai_question_v33.py dry       # вопрос-образец, без модели
    python research/ai_question_v33.py run       # задания на сервер, ответы, итог
    python research/ai_question_v33.py collect   # ответы уже на сервере — забрать и посчитать
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
import ai_question_v32 as V                           # noqa: E402

TAG = 'v33_check'
SAMPLE = 40
RESULT = os.path.join(HERE, 'results', 'ai_question_v33.txt')
SSH_N = R.SSH[:1] + ['-n'] + R.SSH[1:]             # удалённый nohup … & — только с -n, иначе ssh висит

OLD_RISK = ('RISK: at most 6 open positions. Positions opened together in one market move are one bet - prefer the\n'
            'strongest alerts, and skip when your notebook says the conditions are the weak ones.\n')
NEW_RISK = ('RISK: at most 6 open positions. Take every alert that fits your notebook - a liquidation cascade usually gives\n'
            'several good trades at once, and on history taking them all earned more than picking one or two. Skip an\n'
            'alert only when your notebook says its conditions are the weak ones.\n')
NOTEBOOK_V33 = V.NOTEBOOK_V32.replace(OLD_RISK, NEW_RISK)
QUESTION_V33 = V.QUESTION_V32.replace(
    'Answer with JSON only, the reason in at most 25 words:',
    'Answer with JSON only, the reason in Russian, at most 25 words:').replace(
    '"reason": "one sentence"', '"reason": "одно короткое предложение"')
assert NOTEBOOK_V33 != V.NOTEBOOK_V32
assert 'in Russian' in QUESTION_V33 and 'одно короткое предложение' in QUESTION_V33


def plan():
    data, all_s, s_b3, _only_p, s_p, res = Q.sets()
    rng = np.random.default_rng(13)
    cascade = [s for s in all_s if sum(1 for _, k in s['items'] if k == 'P1') >= 3]
    s_c = [cascade[i] for i in sorted(rng.choice(len(cascade), size=min(SAMPLE, len(cascade)), replace=False))]
    pick_b3 = sorted(rng.choice(len(s_b3), size=min(SAMPLE, len(s_b3)), replace=False))
    jobs = ([('c', None, s) for s in s_c] + [('b3', int(i), s_b3[i]) for i in pick_b3]
            + [('p', 83 + j, s) for j, s in enumerate(s_p)])            # номер ответа v3.2 для той же пары
    prompts = [Q.brief_v31(data, s['t'], s['items'], s['held'], s['free'], template=QUESTION_V33)
               for _, _, s in jobs]
    print(f'каскадов всего {len(cascade)}; вопросов модели {len(prompts)} (S_C {len(s_c)}, S_B3 {len(pick_b3)}, '
          f'S_P {len(s_p)})', flush=True)
    return jobs, prompts, res


def raw(question):
    return {'prompt': (f'<|im_start|>system\n{NOTEBOOK_V33}<|im_end|>\n<|im_start|>user\n{question}<|im_end|>\n'
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
    subprocess.run(SSH_N + [f'cd {R.REMOTE} && rm -f {TAG}_answers.jsonl && nohup python3 run_jobs.py '
                            f'{TAG}_jobs.jsonl {TAG}_answers.jsonl > {TAG}_runner.log 2>&1 &'], check=True)
    return B.collect(TAG, len(prompts))


def reason_of(text):
    found = re.search(r'"reason"\s*:\s*"([^"]*)', text)
    return found.group(1) if found else None


def evaluate(jobs, answers, res):
    lines = []

    def say(text=''):
        print(text, flush=True)
        lines.append(text)

    base = [json.loads(x)['content'] for x in open(os.path.join(B.OUT, 'v32_check_answers.jsonl'), encoding='utf-8')]
    rows, bad, reasons, russian = [], 0, 0, 0
    for (kind, idx, s), text in zip(jobs, answers):
        for version, answer in (('v3.3', text), ('v3.2', base[idx] if idx is not None else None)):
            if answer is None:
                continue
            chosen = Q.picks(answer, s['items'])
            if version == 'v3.3':
                bad += chosen is None
                reason = reason_of(answer)
                if reason:
                    reasons += 1
                    russian += bool(re.search('[а-яА-ЯёЁ]', reason))
            chosen = (chosen or [])[:s['free']]
            n_p1 = sum(1 for _, k in s['items'] if k == 'P1')
            for p, k in s['items']:
                rows.append({'set': kind, 'v': version, 'key': k, 'taken': p in chosen, 'r': res[(s['t'], p)][0],
                             'room': min(n_p1, s['free'])})

    import pandas as pd
    d = pd.DataFrame(rows)
    say('Тетрадь и вопрос v3.3 против v3.2 на train + valid (п. 72)')
    for (kind, version, key), g in d.groupby(['set', 'v', 'key']):
        taken = g[g.taken]
        say(f"  {({'c': 'S_C ', 'b3': 'S_B3', 'p': 'S_P '})[kind]} {version} {key}: взято {len(taken)}/{len(g)}, "
            f"R взятых {taken.r.mean() if len(taken) else float('nan'):+.3f} (всех {g.r.mean():+.3f})")

    c = d[(d.set == 'c') & (d.v == 'v3.3') & (d.key == 'P1')]
    rooms = sum(min(n, f) for n, f in _rooms(jobs, 'c'))
    c1 = int(c.taken.sum()) / rooms if rooms else float('nan')
    b = d[(d.set == 'b3') & (d.v == 'v3.3') & (d.key == 'B3')]
    c2 = b.taken.mean()
    p_new, p_old = d[(d.set == 'p') & (d.v == 'v3.3')], d[(d.set == 'p') & (d.v == 'v3.2')]
    c3a = abs(p_new[p_new.key == 'P2'].taken.mean() - p_old[p_old.key == 'P2'].taken.mean())
    c3b = p_new[p_new.taken].r.mean() - p_old[p_old.taken].r.mean()
    c4 = bad / len(answers)
    c5 = russian / reasons if reasons else float('nan')
    checks = [(f'1. S_C: v3.3 берёт P1 {c1 * 100:.0f}% возможного (нужно ≥ 70%)', c1 >= 0.70),
              (f'2. S_B3: берёт B3 {c2 * 100:.0f}% (нужно ≥ 50%)', c2 >= 0.50),
              (f'3. S_P: доля P2 v3.3 − v3.2 {c3a * 100:.0f} п. п. (нужно ≤ 15); R взятых v3.3 − v3.2 {c3b:+.3f} '
               f'(нужно ≥ −0.10)', c3a <= 0.15 and c3b >= -0.10),
              (f'4. без целого JSON {c4 * 100:.1f}% (нужно ≤ 5%)', c4 <= 0.05),
              (f'5. причина по-русски {c5 * 100:.0f}% (нужно ≥ 90%)', c5 >= 0.90)]
    say()
    for text, ok in checks:
        say(f"  {'да ' if ok else 'НЕТ'} {text}")
    verdict = all(ok for _, ok in checks)
    say(f"\nИтог: v3.3 {'принят — идёт вживую' if verdict else 'не принят — остаётся v3.2'}")
    say('\nПримеры причин v3.3:')
    for text in answers[:5]:
        say(f'  {reason_of(text)}')
    with open(RESULT, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return verdict


def _rooms(jobs, kind):
    """(сигналов P1, свободно мест) по срезам набора."""
    return [(sum(1 for _, k in s['items'] if k == 'P1'), s['free']) for kd, _, s in jobs if kd == kind]


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    mode = sys.argv[1] if len(sys.argv) > 1 else 'dry'
    jobs, prompts, res = plan()
    if mode == 'dry':
        print(raw(prompts[0])['prompt'][-1800:])
    else:
        answers = B.collect(TAG, len(prompts)) if mode == 'collect' else ask(prompts)
        evaluate(jobs, answers, res)
