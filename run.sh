#!/usr/bin/env bash
# Запуск бота в текущем окне. Остановка — Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")"

[ -d venv ] || { echo "Окружение не создано. Сначала: ./install.sh" >&2; exit 1; }
export BOT_DATA_DIR="$(pwd)/bot_data"
# shellcheck disable=SC1091
source venv/bin/activate
exec python Live_Bot/bot.py
