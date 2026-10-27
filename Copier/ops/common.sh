#!/usr/bin/env bash
# Общие пути и помощники скриптов копировщика. Подключается через source.

COPIER=/opt/copier
CODE=$COPIER/code                 # служба копировщика (Copier/)
ENGINE=$COPIER/engine             # замороженный движок (копия бота на теге)
ENGINE_DATA=$COPIER/engine_data
DATA=$COPIER/data
VENV=$COPIER/venv
ENGINE_VENV=$COPIER/engine_venv
CRED=$COPIER/.git-credentials     # для git-режима deploy.sh (если код — клон с GitHub)
LOG=$DATA/ops.log
ADMIN_URL=http://127.0.0.1:8790

say() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG" >&2; }
die() { say "ОШИБКА: $*"; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "запускать от root"; }

git_c() { git -c credential.helper="store --file=$CRED" "$@"; }

admin_token() { sed -n 's/^COPIER_ADMIN_TOKEN=//p' "$DATA/.env" 2>/dev/null; }

admin_post() {   # admin_post /api/system '{"action":"pause_entries"}'
  local token; token=$(admin_token)
  [ -n "$token" ] || return 1
  curl -s -m 10 -X POST -H "X-Admin-Token: $token" -H 'Content-Type: application/json' \
       -d "$2" "$ADMIN_URL$1"
}

entries_paused() {
  local token; token=$(admin_token)
  curl -s -m 10 -H "X-Admin-Token: $token" "$ADMIN_URL/api/overview" | grep -q '"entries_paused": true'
}

# Снимок настроек торгового бота для движка: .env без секретов + настройки
# стратегий и тестовых счетов. Делается ТОЛЬКО по команде владельца (setup,
# engine_update --with-settings): настройки — часть замороженной версии.
snapshot_settings() {
  local src=/opt/kraken/bot_data
  [ -d "$src" ] || die "нет $src — снимок настроек взять неоткуда"
  local tmp; tmp=$(mktemp)
  {
    echo "# Снимок настроек торгового бота $(date -u +%FT%TZ) — без секретов. Потом — engine.env."
    grep -E '^[A-Z0-9_]+=' "$src/.env" 2>/dev/null \
      | grep -vE '^(TELEGRAM_|BYBIT_|BINGX_|.*_API_KEY|.*_SECRET|.*SECRET_KEY|LLM_|DASHBOARD_|PLATFORM_|ADMIN_IDS|PAYMENT_|NOWPAYMENTS|PM_|TRADING_MODE|LIVE_CONFIRMED|EXCHANGE_LIVE_ENABLED|BOT_DATA_DIR|REMOTE_)' \
      || true
    echo
    cat "$CODE/Copier/ops/engine.env.example"
  } > "$tmp"
  install -o copier -g copier -m 600 "$tmp" "$ENGINE_DATA/.env"
  rm -f "$tmp"
  for f in runtime_settings.json accounts.json; do
    if [ -f "$src/$f" ]; then
      install -o copier -g copier -m 600 "$src/$f" "$ENGINE_DATA/$f"
    fi
  done
  # Анализ дня моделью в движке не нужен: модели у движка нет.
  ( cd "$ENGINE/Live_Bot" && sudo -u copier env BOT_DATA_DIR="$ENGINE_DATA" HOME="$ENGINE_DATA" \
      "$ENGINE_VENV/bin/python" -c "from accounts import settings_store; settings_store.save({'DAILY': {'enabled': False}})" ) \
    || say "внимание: анализ дня в движке не выключен — проверьте журнал движка"
  say "настройки торгового бота сняты в движок (без секретов)"
}
