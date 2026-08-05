#!/usr/bin/env bash
# Установка бота на сервер. Запускать из папки с этим файлом: ./install.sh
#
# Скрипт идемпотентный: повторный запуск ничего не ломает и не перезаписывает
# ни .env, ни журнал сделок. Его можно смело гонять после каждого обновления.
set -euo pipefail
cd "$(dirname "$0")"

APP_DIR="$(pwd)"
DATA_DIR="$APP_DIR/bot_data"
VENV="$APP_DIR/venv"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mВНИМ\033[0m %s\n' "$*"; }
die()  { printf '  \033[31mОШИБКА\033[0m %s\n' "$*" >&2; exit 1; }

say "1. Python"
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$candidate"; break
        fi
    fi
done
[ -n "$PY" ] || die "нужен Python 3.10 или новее. Ubuntu: sudo apt install python3 python3-venv"
ok "$($PY --version)"

say "2. Виртуальное окружение"
if [ ! -d "$VENV" ]; then
    "$PY" -m venv "$VENV" || die "не удалось создать venv. Ubuntu: sudo apt install python3-venv"
    ok "создано: $VENV"
else
    ok "уже есть: $VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

say "3. Зависимости"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || die "не установились зависимости"
ok "установлены из requirements.txt"

say "4. Каталог данных"
# Данные лежат ОТДЕЛЬНО от кода. Это единственное, что нельзя терять:
# журнал сделок, состояние позиций, ключи и настройки оператора. Папку с
# кодом можно перезаписывать целиком — bot_data это не заденет.
mkdir -p "$DATA_DIR"
ok "$DATA_DIR"

say "5. Настройки"
if [ ! -f "$DATA_DIR/.env" ]; then
    cp .env.example "$DATA_DIR/.env"
    chmod 600 "$DATA_DIR/.env"
    warn "создан $DATA_DIR/.env из шаблона — ВПИШИТЕ КЛЮЧИ БИРЖИ"
    warn "потом запустите ./install.sh ещё раз для проверки"
    NEED_KEYS=1
else
    ok ".env на месте (не тронут)"
    NEED_KEYS=0
fi

say "6. Проверка готовности"
if [ "$NEED_KEYS" = "1" ]; then
    warn "пропущена: сначала заполните .env"
    echo
    echo "Дальше: отредактируйте $DATA_DIR/.env, затем ./install.sh"
    exit 0
fi

export BOT_DATA_DIR="$DATA_DIR"
python Live_Bot/doctor.py || die "проверка не пройдена — см. список выше"

say "Готово"
cat <<EOF
  Запуск в текущем окне:      ./run.sh
  Запуск службой (systemd):   sudo ./install_service.sh
  Дашборд:                    http://localhost:8787

  Данные:   $DATA_DIR   (обновление кода их не трогает)
EOF
