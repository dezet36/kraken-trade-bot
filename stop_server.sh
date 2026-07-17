#!/usr/bin/env bash
# Остановка Kraken-бота: ./stop_server.sh
set -euo pipefail
cd "$(dirname "$0")"
docker compose down
echo "Остановлено. Состояние/журналы сохранены в bot_data/ (том на хосте)."
