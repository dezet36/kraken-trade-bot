#!/usr/bin/env bash
# Запуск Kraken-бота на сервере одним файлом: ./start_server.sh
# Требования: Docker + docker compose plugin; заполненный Live_Bot/.env
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f Live_Bot/.env ]; then
    echo "ОШИБКА: нет Live_Bot/.env (ключи биржи/Telegram). Скопируй его на сервер." >&2
    exit 1
fi

docker compose up -d --build kraken-trader
echo "── Статус ──"
docker compose ps
echo "Логи:   docker compose logs -f kraken-trader"
echo "Стоп:   ./stop_server.sh"
