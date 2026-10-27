#!/usr/bin/env bash
# Первая установка копировщика на сервер. Запуск от root:
#   bash /opt/copier/code.new/Copier/ops/setup_server.sh <telegram-id-владельца> [коммит-движка]
# Код копировщика заливается заранее: ops/upload.sh --only (или git clone в /opt/copier/code).
#
# Что делает (торговый бот, его службы, .env и файлы НЕ меняются — только читаются:
# его git-репозиторий как источник кода движка и снимок настроек стратегий):
#   1. пользователь copier без входа;
#   2. /opt/copier/{code,engine,engine_data,data,venv,engine_venv};
#   3. движок — свой клон кода бота на коммите (по умолчанию тот, что бот выполняет
#      сейчас), отсоединённый HEAD + локальный тег engine-<дата>-<коммит>;
#   4. окружения Python раздельно: у движка — по requirements своей версии;
#   5. .env копировщика с новыми секретами, .env движка — снимок без секретов;
#   6. службы copier-engine (сразу) и copier (если вписан COPIER_TG_TOKEN);
#   7. cron: сведения о движке раз в час, резервная копия базы раз в сутки.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p /opt/copier/data
source "$HERE/common.sh"
need_root

ADMIN_ID=${1:?укажите Telegram id владельца}
REF=${2:-}
ENGINE_SRC=${ENGINE_SRC:-/opt/kraken/code}

if [ ! -d "$CODE/Copier" ]; then
  [ -d "$COPIER/code.new/Copier" ] || die "нет кода копировщика: сначала ops/upload.sh --only"
  mv "$COPIER/code.new" "$CODE"
fi

id copier >/dev/null 2>&1 || useradd --system --home-dir "$COPIER" --shell /usr/sbin/nologin copier
mkdir -p "$ENGINE_DATA" "$DATA" "$COPIER/backups"

if [ ! -d "$ENGINE/.git" ]; then
  git clone -q "$ENGINE_SRC" "$ENGINE"
fi
git -C "$ENGINE" fetch -q origin
REF=${REF:-$(git -C "$ENGINE_SRC" rev-parse HEAD)}
git -C "$ENGINE" -c advice.detachedHead=false checkout -q --detach "$REF"
TAG=engine-$(date -u +%Y-%m-%d)-$(git -C "$ENGINE" rev-parse --short HEAD)
git -C "$ENGINE" tag -f "$TAG" >/dev/null
say "движок: $TAG — $(git -C "$ENGINE" log --oneline -1)"

[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q -r "$CODE/Copier/requirements.txt"
[ -x "$ENGINE_VENV/bin/python" ] || python3 -m venv "$ENGINE_VENV"
"$ENGINE_VENV/bin/python" -m pip install -q -r "$ENGINE/requirements.txt"

chown -R root:root "$CODE" "$ENGINE"
chown -R copier:copier "$DATA" "$ENGINE_DATA" "$COPIER/backups"
chmod 700 "$DATA" "$ENGINE_DATA" "$COPIER/backups"

if [ ! -f "$DATA/.env" ]; then
  SECRET=$("$VENV/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  TOKEN=$(openssl rand -hex 24)
  sed -e "s|^COPIER_ADMIN_IDS=.*|COPIER_ADMIN_IDS=$ADMIN_ID|" \
      -e "s|^COPIER_SECRET_KEY=.*|COPIER_SECRET_KEY=$SECRET|" \
      -e "s|^COPIER_ADMIN_TOKEN=.*|COPIER_ADMIN_TOKEN=$TOKEN|" \
      "$CODE/Copier/ops/copier.env.example" > "$DATA/.env"
  chown copier:copier "$DATA/.env"
  chmod 600 "$DATA/.env"
  say "создан $DATA/.env — впишите COPIER_TG_TOKEN и сохраните COPIER_SECRET_KEY отдельно"
fi

snapshot_settings

install -m 644 "$CODE/Copier/ops/copier-engine.service" /etc/systemd/system/
install -m 644 "$CODE/Copier/ops/copier.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now copier-engine
if grep -qE '^COPIER_TG_TOKEN=.+' "$DATA/.env"; then
  systemctl enable --now copier
else
  systemctl enable copier
  say "служба copier включена, но не запущена: нет COPIER_TG_TOKEN (после вписывания — systemctl start copier)"
fi

cat > /etc/cron.d/copier <<EOF
# Копировщик: сведения о версии движка и резервная копия базы участников.
17 * * * * root bash $CODE/Copier/ops/engine_info.sh >/dev/null 2>&1
35 3 * * * root bash $CODE/Copier/ops/backup.sh >/dev/null 2>&1
EOF
bash "$CODE/Copier/ops/engine_info.sh" || true

say "готово. Проверка: systemctl status copier-engine copier; journalctl -u copier-engine -n 50"
