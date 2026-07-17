#!/usr/bin/env bash
# Запуск Kraken-бота на сервере одним файлом: ./start_server.sh
# Требования: Docker + docker compose plugin; заполненный .env (в Live_Bot/ или bot_data/)
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p bot_data

# ── Разовая миграция: .env и любые существующие данные из Live_Bot/ -> bot_data/ ──
# (защита от копирования: Live_Bot/ теперь безопасно перезаписывать целиком, а
# bot_data/ живёт отдельно и копированием Live_Bot больше не затрагивается)
if [ ! -f bot_data/.env ] && [ -f Live_Bot/.env ]; then
    echo "Первый запуск с новой схемой: переношу .env и данные в bot_data/ (разово)..."
    mv Live_Bot/.env bot_data/.env
    for f in platform.db trades_journal.csv trades_detail.jsonl \
             cooldown_state.json positions_state.json pending_orders.json \
             bot_log.txt trades.csv; do
        [ -e "Live_Bot/$f" ] && mv "Live_Bot/$f" "bot_data/$f"
    done
    [ -d Live_Bot/state ] && mv Live_Bot/state bot_data/state
    echo "Готово. Дальше папку Live_Bot можно заменять целиком — bot_data/ не пострадает."
fi

if [ ! -f bot_data/.env ]; then
    echo "ОШИБКА: нет bot_data/.env (ключи биржи/Telegram). Помести .env в bot_data/." >&2
    exit 1
fi

docker compose up -d --build kraken-trader
echo "── Статус ──"
docker compose ps
echo "Логи:   docker compose logs -f kraken-trader"
echo "Стоп:   ./stop_server.sh"
