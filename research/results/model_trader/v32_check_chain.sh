#!/usr/bin/env bash
# Прогон v3.1 обрывается после ответов v3 на S_P (п. 70), затем — задания v3.2.
cd /opt/kraken/llm_exp/patterns
P=v31_check_answers.jsonl.part
until [ "$(wc -l < $P 2>/dev/null || echo 0)" -ge 123 ]; do sleep 15; done
pid=$(pgrep -f "^python3 run_jobs.py v31_check_jobs.jsonl")
[ -n "$pid" ] && kill $pid
head -n 123 $P > v31_check_partial.jsonl
rm -f v32_check_answers.jsonl
nohup python3 run_jobs.py v32_check_jobs.jsonl v32_check_answers.jsonl > v32_check_runner.log 2>&1 &
echo "$(date -u +%FT%TZ) v3.1 оборван (PID $pid) на $(wc -l < v31_check_partial.jsonl) ответах, v3.2 запущен"
