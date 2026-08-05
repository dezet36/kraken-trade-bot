#!/usr/bin/env bash
# Установка службы systemd: бот стартует при загрузке и поднимается после падения.
# Запускать от root: sudo ./install_service.sh
set -euo pipefail
cd "$(dirname "$0")"

[ "$(id -u)" = "0" ] || { echo "Нужен root: sudo ./install_service.sh" >&2; exit 1; }
APP_DIR="$(pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"
UNIT=/etc/systemd/system/kraken-bot.service

[ -d "$APP_DIR/venv" ] || { echo "Сначала ./install.sh" >&2; exit 1; }

cat > "$UNIT" <<UNITEOF
[Unit]
Description=Kraken trading bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=BOT_DATA_DIR=$APP_DIR/bot_data
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/Live_Bot/bot.py
# Перезапуск после падения. Пауза нужна, чтобы при неустранимой ошибке
# (неверный ключ) служба не крутила рестарт в цикле, забивая журнал.
Restart=always
RestartSec=30
StartLimitBurst=5
StartLimitIntervalSec=600

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable kraken-bot
systemctl restart kraken-bot
sleep 2
systemctl --no-pager status kraken-bot | head -12
cat <<MSG

Служба установлена.
  Статус:     systemctl status kraken-bot
  Логи:       journalctl -u kraken-bot -f
  Стоп:       systemctl stop kraken-bot
  Перезапуск: systemctl restart kraken-bot   (нужен после обновления кода)
MSG
