"""
Вопрос v3.2 против v3 на истории (28.09.2026, docs/ИИ_замечания_на_проверку.md, п. 70).

v3.1 (ключ "trade", сторона в строке сигнала, BTC за 30 дней, «каждый сигнал уже
выполнил условие») не прошёл по п. 4: модель рассуждает в «reason» и упирается в
n_predict. v3.2 = v3.1 + причина не длиннее 25 слов + понятная шкала ранга доли
розницы (0 = lowest, 1 = highest) и порог «high» в тетради (ранг > 0.5).
Закономерности и исполнение — без изменений. Условия приёмки — п. 70, до прогона.

Наборы срезов те же, что у v3.1 (ai_question_v31.sets). Основа на S_P — ответы v3
из прогона v3.1: он обрывается, как только ответит на 123-й вопрос (83 S_B3 v3.1 +
40 S_P v3), и сразу стартует v3.2 — это делает цепочка на сервере.

    python research/ai_question_v32.py dry       # вопрос-образец, без модели
    python research/ai_question_v32.py queue     # задания и цепочка на сервер
    python research/ai_question_v32.py collect   # дождаться ответов и посчитать
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ai_model_research as R                         # noqa: E402
import ai_model_trader_bt as B                        # noqa: E402
import ai_question_v31 as Q                           # noqa: E402

TAG = 'v32_check'
V31_DONE = 123            # ответов прогона v3.1 до конца ответов v3 на S_P
RESULT = os.path.join(HERE, 'results', 'ai_question_v32.txt')

NOTEBOOK_V32 = Q.NOTEBOOK_V3.replace(
    'Better: retail long share high (+0.35R, the crowd is buying the bounce)',
    'Better: retail long share high (30-day rank above 0.5: +0.35R, the crowd is buying the bounce)')
QUESTION_V32 = Q.QUESTION_V31.replace(
    'retail long share percentile 30d:',
    'retail long share rank vs its last 30 days (0 = lowest, 1 = highest):').replace(
    'Answer with JSON only:', 'Answer with JSON only, the reason in at most 25 words:')
assert NOTEBOOK_V32 != Q.NOTEBOOK_V3
assert 'rank vs its last 30 days' in QUESTION_V32 and 'at most 25 words' in QUESTION_V32

CHAIN = r'''#!/usr/bin/env bash
# Прогон v3.1 обрывается после ответов v3 на S_P (п. 70), затем — задания v3.2.
cd {remote}
P=v31_check_answers.jsonl.part
until [ "$(wc -l < $P 2>/dev/null || echo 0)" -ge {done} ]; do sleep 15; done
pid=$(pgrep -f "^python3 run_jobs.py v31_check_jobs.jsonl")
[ -n "$pid" ] && kill $pid
head -n {done} $P > v31_check_partial.jsonl
rm -f {tag}_answers.jsonl
nohup python3 run_jobs.py {tag}_jobs.jsonl {tag}_answers.jsonl > {tag}_runner.log 2>&1 &
echo "$(date -u +%FT%TZ) v3.1 оборван (PID $pid) на $(wc -l < v31_check_partial.jsonl) ответах, v3.2 запущен"
'''


def plan():
    data, _all, s_b3, _only_p, s_p, res = Q.sets()
    jobs = [('b3', 'v32', s) for s in s_b3] + [('p', 'v32', s) for s in s_p]
    prompts = [Q.brief_v31(data, s['t'], s['items'], s['held'], s['free'], template=QUESTION_V32)
               for _, _, s in jobs]
    base = [('b3', 'v31', s) for s in s_b3] + [('p', 'v3', s) for s in s_p]      # первые 123 задания v3.1
    assert len(base) == V31_DONE, len(base)
    return jobs, prompts, base, res


def raw(question):
    return {'prompt': (f'<|im_start|>system\n{NOTEBOOK_V32}<|im_end|>\n<|im_start|>user\n{question}<|im_end|>\n'
                       f'<|im_start|>assistant\n<think>\n\n</think>\n\n'),
            'n_predict': 160, 'temperature': 0.2, 'top_k': 20, 'top_p': 0.9, 'stop': ['<|im_end|>'],
            'cache_prompt': True}


def queue(prompts):
    os.makedirs(B.OUT, exist_ok=True)
    local = os.path.join(B.OUT, f'{TAG}_jobs.jsonl')
    with open(local, 'w', encoding='utf-8', newline='\n') as fh:
        for q in prompts:
            fh.write(json.dumps(raw(q), ensure_ascii=False) + '\n')
    chain = os.path.join(B.OUT, f'{TAG}_chain.sh')
    with open(chain, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(CHAIN.format(remote=R.REMOTE, done=V31_DONE, tag=TAG))
    subprocess.run(R.SCP + [local, chain, f'root@195.133.35.5:{R.REMOTE}/'], check=True)
    subprocess.run(R.SSH + [f'cd {R.REMOTE} && nohup bash {TAG}_chain.sh > {TAG}_chain.log 2>&1 &'], check=True)
    print(f'заданий v3.2: {len(prompts)}; цепочка на сервере ждёт {V31_DONE}-й ответ v3.1', flush=True)


def fetch(name):
    out = subprocess.run(R.SSH + [f'cat {R.REMOTE}/{name}'], capture_output=True, text=True, encoding='utf-8',
                         check=True).stdout
    open(os.path.join(B.OUT, name), 'w', encoding='utf-8').write(out)
    return [json.loads(x)['content'] for x in out.strip().splitlines()]


def listed(text):
    """Справочно: список сделок есть, даже если ответ обрезан на причине (так читает живой llm_notebook)."""
    return bool(re.search(r'"(?:trade|buy)"\s*:\s*\[[^\]]*\]', text))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    mode = sys.argv[1] if len(sys.argv) > 1 else 'dry'
    jobs, prompts, base, res = plan()
    if mode == 'dry':
        print(prompts[0])
    elif mode == 'queue':
        queue(prompts)
    elif mode == 'collect':
        answers = B.collect(TAG, len(prompts))
        partial = fetch('v31_check_partial.jsonl')
        Q.evaluate(base + jobs, partial + answers, res, cand='v32', name='v3.2', result=RESULT,
                   title='Вопрос v3.2 против v3 на train + valid (п. 70); строка S_B3 v31 — справочно')
        for name, got in (('v3.1 (S_B3)', partial[:len(base) - 40]), ('v3.2', answers)):
            cut = sum(1 for t in got if not listed(t))
            print(f'  справочно, {name}: без целого списка сделок {cut} из {len(got)}', flush=True)
