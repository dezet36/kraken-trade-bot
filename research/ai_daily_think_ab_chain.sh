#!/bin/bash
# Цепочка сравнения «с мыслью / без» (п. 88а) на сервере — одна ночь:
# ждёт боевые разборы дня BTC, ETH и SOL за DAY, забирает их как «без мысли»
# (не удался — собирает вопрос заново), собирает вопрос XRP и
# запускает прогон. Запуск: setsid nohup bash research/ai_daily_think_ab_chain.sh 2026-10-15 &
DAY=${1:?день боевых разборов, ГГГГ-ММ-ДД}
cd /opt/kraken/code/Live_Bot || exit 1
export BOT_DATA_DIR=/opt/kraken/bot_data
PY=/opt/kraken/venv/bin/python
AB=../research/ai_daily_think_ab.py
REPORTS=$BOT_DATA_DIR/daily_reports
for i in $(seq 1 240); do                        # до 4 ч ожидания
    [ -f "$REPORTS/${DAY}_BTCUSDT.json" ] && [ -f "$REPORTS/${DAY}_ETHUSDT.json" ] \
        && [ -f "$REPORTS/${DAY}_SOLUSDT.json" ] && break
    sleep 60
done
$PY $AB import "$DAY" BTCUSDT ETHUSDT SOLUSDT
for p in BTCUSDT ETHUSDT SOLUSDT XRPUSDT; do
    [ -f ../research/results/ai_daily_ab/q_$p.txt ] || $PY $AB collect $p
done
$PY $AB run
$PY $AB report
