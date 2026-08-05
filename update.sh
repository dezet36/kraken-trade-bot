#!/usr/bin/env bash
# Обновление кода копированием новой версии поверх старой.
#
#   ./update.sh /path/to/new-release      папка с новой версией
#   ./update.sh /path/to/kraken-bot.zip   архив
#
# Нужен, когда на сервере нет git или доступа к GitHub. Если папка бота —
# git-репозиторий, проще нажать «Обновить» на дашборде.
#
# Каталог bot_data НЕ ТРОГАЕТСЯ: ключи, журнал сделок, состояние позиций и
# настройки переживают обновление. Старый код сохраняется рядом — если новая
# версия окажется нерабочей, есть куда вернуться.
set -euo pipefail
cd "$(dirname "$0")"

APP_DIR="$(pwd)"
SRC="${1:-}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$APP_DIR/.backup-$STAMP"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
die()  { printf '  \033[31mОШИБКА\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "$SRC" ] || die "укажите путь: ./update.sh /path/to/new-release"

say "1. Проверка новой версии"
TMP=""
if [ -f "$SRC" ]; then
    TMP="$(mktemp -d)"
    unzip -q "$SRC" -d "$TMP" || die "не распаковался архив"
    SRC="$TMP"
    # Архив мог быть собран с папкой внутри
    [ -f "$SRC/Live_Bot/bot.py" ] || SRC="$(find "$TMP" -maxdepth 2 -name bot.py -path '*/Live_Bot/*' -printf '%h\n' | head -1 | xargs dirname)"
fi
[ -f "$SRC/Live_Bot/bot.py" ] || die "в $SRC не видно Live_Bot/bot.py — это не сборка бота"
[ -f "$SRC/requirements.txt" ] || die "в $SRC нет requirements.txt"
ok "$SRC"

say "2. Резервная копия старого кода"
mkdir -p "$BACKUP"
cp -r "$APP_DIR/Live_Bot" "$BACKUP/Live_Bot"
for f in requirements.txt install.sh run.sh install.ps1 run.ps1; do
    [ -f "$APP_DIR/$f" ] && cp "$APP_DIR/$f" "$BACKUP/$f"
done
ok "$BACKUP"

say "3. Замена кода"
# Данные лежат в bot_data/ и в список замены не входят вовсе — их физически
# нечем задеть.
rm -rf "$APP_DIR/Live_Bot"
cp -r "$SRC/Live_Bot" "$APP_DIR/Live_Bot"
for f in requirements.txt install.sh run.sh install_service.sh install.ps1 run.ps1 \
         .env.example README_СЕРВЕР.md DEPLOY.md Dockerfile docker-compose.yml; do
    [ -f "$SRC/$f" ] && cp "$SRC/$f" "$APP_DIR/$f"
done
chmod +x "$APP_DIR"/*.sh 2>/dev/null || true
ok "код заменён, bot_data не тронут"
[ -n "$TMP" ] && rm -rf "$TMP"

say "4. Зависимости"
if [ -d "$APP_DIR/venv" ]; then
    "$APP_DIR/venv/bin/python" -m pip install --quiet -r requirements.txt \
        && ok "обновлены" || die "не установились — верните код из $BACKUP"
else
    ok "venv нет, пропускаем (запустите ./install.sh)"
fi

say "5. Проверка готовности"
export BOT_DATA_DIR="$APP_DIR/bot_data"
PY="$APP_DIR/venv/bin/python"; [ -x "$PY" ] || PY=python3
if ! "$PY" Live_Bot/doctor.py; then
    echo
    die "новая версия не проходит проверку. Вернуть старую:
       rm -rf Live_Bot && cp -r $BACKUP/Live_Bot Live_Bot"
fi

say "Готово"
cat <<EOF
  Перезапустите бота, иначе работает ещё старый код:
      systemctl restart kraken-bot      (если ставили службой)

  Резервная копия старого кода: $BACKUP
  Убедитесь, что всё работает, потом удалите её.
EOF
