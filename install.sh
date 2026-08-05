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

# Значение подставляется в СУЩЕСТВУЮЩУЮ строку, а не дописывается в конец:
# иначе в файле окажутся два TRADING_MODE, и какой из них подействует — вопрос
# порядка чтения, а не намерения.
set_env() {
    local file="$1" key="$2" value="$3"
    if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
        python - "$file" "$key" "$value" <<'PY'
import io, re, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
text = io.open(path, encoding='utf-8').read()
text = re.sub(r'(?m)^\s*%s\s*=.*$' % re.escape(key), '%s=%s' % (key, value), text)
io.open(path, 'w', encoding='utf-8').write(text)
PY
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

ask_choice() {   # ask_choice "Заголовок" "вариант1 вариант2" "поумолчанию"
    local title="$1" options="$2" default="$3" answer
    while :; do
        printf "  %s [%s], по умолчанию %s: " "$title" "${options// /\/}" "$default" >&2
        read -r answer
        [ -z "$answer" ] && { printf '%s' "$default"; return; }
        for o in $options; do
            [ "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" = \
              "$(printf '%s' "$o" | tr '[:upper:]' '[:lower:]')" ] && { printf '%s' "$o"; return; }
        done
        echo "    надо одно из: ${options// /, }" >&2
    done
}

ask_secret() {
    local title="$1" value
    while :; do
        printf "  %s: " "$title" >&2
        read -rs value; echo >&2
        # Ключ из браузера часто приезжает с пробелом на конце, и биржа
        # отвечает «неверная подпись» — искать причину потом долго.
        value="$(printf '%s' "$value" | tr -d '[:space:]')"
        [ -n "$value" ] && { printf '%s' "$value"; return; }
        echo "    пусто — введите значение" >&2
    done
}

say "5. Настройки"
if [ ! -f "$DATA_DIR/.env" ]; then
    cp .env.example "$DATA_DIR/.env"
    chmod 600 "$DATA_DIR/.env"
    # Спрашиваем ключи здесь же. Отправлять человека править .env в редакторе —
    # лишний шаг, на котором проще всего ошибиться: не тот файл, лишние пробелы,
    # кавычки вокруг ключа. Запишем сами. Под systemd и в конвейере терминала
    # нет — там остаётся прежний путь с подсказкой, иначе установка встала бы
    # насмерть на вопросе, которого никто не видит.
    if [ -t 0 ]; then
        echo
        echo "  Нужны ключи биржи. Даже в режиме фантома: котировки берутся с биржи."
        echo "  Для PAPER и DEMO подойдут ключи демо-счёта."
        echo
        EXCHANGE="$(ask_choice 'Биржа' 'bybit bingx' 'bybit')"
        MODE="$(ask_choice 'Режим' 'PAPER DEMO LIVE' 'PAPER')"
        [ "$MODE" = "LIVE" ] && warn "LIVE — реальные деньги. Бот переспросит при запуске."
        PREFIX="$(printf '%s' "$EXCHANGE" | tr '[:lower:]' '[:upper:]')"
        API_KEY="$(ask_secret "$PREFIX API key")"
        API_SECRET="$(ask_secret "$PREFIX secret key")"

        set_env "$DATA_DIR/.env" 'EXCHANGE' "$EXCHANGE"
        set_env "$DATA_DIR/.env" 'TRADING_MODE' "$MODE"
        set_env "$DATA_DIR/.env" "${PREFIX}_API_KEY" "$API_KEY"
        set_env "$DATA_DIR/.env" "${PREFIX}_SECRET_KEY" "$API_SECRET"
        ok "записаны в $DATA_DIR/.env"
        NEED_KEYS=0
    else
        warn "создан $DATA_DIR/.env из шаблона — ВПИШИТЕ КЛЮЧИ БИРЖИ"
        warn "потом запустите ./install.sh ещё раз для проверки"
        NEED_KEYS=1
    fi
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

# Проверка пройдена — предложить сразу и запустить. Иначе установка кончается
# списком команд, которые надо где-то набрать, а просили обратного.
if [ -t 0 ]; then
    echo
    HOW="$(ask_choice 'Запустить сейчас' 'служба окно нет' 'служба')"
    case "$HOW" in
        служба) sudo ./install_service.sh ;;
        окно)   ./run.sh ;;
    esac
fi
