#!/usr/bin/env bash
#
# Один файл, с которого начинается установка на сервер.
#
# Скачивает актуальный код с GitHub, ставит окружение и зависимости, заводит
# каталог данных и прогоняет проверку готовности. Больше ничего копировать
# на сервер не нужно — только этот файл.
#
# ПОЧЕМУ ОН НУЖЕН ОТДЕЛЬНО ОТ install.sh. install.sh ставит УЖЕ скопированную
# папку: он умеет создать venv и проверить настройки, но взять код ему
# неоткуда. Здесь наоборот: кода на сервере ещё нет, и первый шаг — принести
# его с GitHub. Дальше управление передаётся install.sh, чтобы не иметь двух
# разных установщиков, расходящихся со временем.
#
# ПОВТОРНЫЙ ЗАПУСК БЕЗОПАСЕН. Если папка уже существует и является
# репозиторием, код обновляется, а каталог данных не трогается вовсе — он
# лежит вне контроля версий (bot_data/ в .gitignore), git его не видит.
#
# Запуск:
#     chmod +x bootstrap.sh
#     ./bootstrap.sh                    -> /opt/kraken-bot
#     ./bootstrap.sh ~/kraken-bot       -> куда скажете

set -euo pipefail

REPO_URL="https://github.com/dezet36/kraken-trade-bot.git"
BRANCH="main"
DIR="${1:-/opt/kraken-bot}"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
ok()   { echo "  ${GREEN}✓${OFF} $*"; }
info() { echo "  ${DIM}$*${OFF}"; }
warn() { echo "  ${YELLOW}!${OFF} $*"; }
die()  { echo "  ${RED}✗ $*${OFF}" >&2; exit 1; }

echo
echo "── Установка торгового бота ────────────────────────────────────────────"
echo "   репозиторий: $REPO_URL ($BRANCH)"
echo "   каталог:     $DIR"
echo

# ── Что должно быть на сервере ───────────────────────────────────────────────
command -v git >/dev/null 2>&1 \
    || die "нужен git. Ubuntu/Debian: sudo apt install git | CentOS: sudo yum install git"
ok "$(git --version)"

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PY="$candidate"; break
    fi
done
[ -n "$PY" ] || die "нужен Python 3.10 или новее. Ubuntu: sudo apt install python3 python3-venv"
ok "$($PY --version)"

# Каталог создаём заранее: доступ к нему нужно проверить ДО того, как мы
# полчаса будем что-то качать и только потом упрёмся в права.
PARENT="$(dirname "$DIR")"
mkdir -p "$PARENT" 2>/dev/null || die "нет доступа к $PARENT — запустите через sudo или укажите другой каталог"
[ -w "$PARENT" ] || die "нет прав на запись в $PARENT — запустите через sudo или укажите другой каталог"

# ── Доступ к репозиторию ─────────────────────────────────────────────────────
# Репозиторий закрытый, поэтому нужен токен. Сначала всё же пробуем без него:
# если репозиторий когда-нибудь откроют, лишний вопрос будет только мешать.
CRED_FILE="$DIR/bot_data/.git-credentials"
NEED_TOKEN=1

if GIT_TERMINAL_PROMPT=0 git ls-remote "$REPO_URL" HEAD >/dev/null 2>&1; then
    NEED_TOKEN=0
    ok "репозиторий доступен без токена"
elif [ -f "$CRED_FILE" ] && GIT_TERMINAL_PROMPT=0 \
        git -c "credential.helper=store --file=$CRED_FILE" \
            ls-remote "$REPO_URL" HEAD >/dev/null 2>&1; then
    NEED_TOKEN=0
    ok "токен уже сохранён с прошлой установки"
fi

if [ "$NEED_TOKEN" = 1 ]; then
    echo
    echo "  Репозиторий закрытый — нужен токен доступа GitHub."
    echo
    echo "  Где взять: github.com -> Settings -> Developer settings ->"
    echo "             Personal access tokens -> Fine-grained tokens -> Generate"
    echo "             Repository access: только kraken-trade-bot"
    echo "             Permissions: Contents -> Read-only"
    echo
    echo "  Токен сохранится в $CRED_FILE (права 600) и понадобится ещё раз"
    echo "  при обновлениях — вводить его каждый раз не придётся."
    echo
    printf "  Токен: "
    read -rs TOKEN
    echo
    [ -n "$TOKEN" ] || die "токен не введён"

    mkdir -p "$DIR/bot_data"
    # umask в подоболочке: иначе он остался бы до конца скрипта и урезал бы
    # права всему, что скачается следом.
    ( umask 077
      printf 'https://x-access-token:%s@github.com\n' "$TOKEN" > "$CRED_FILE" )
    chmod 600 "$CRED_FILE"

    GIT_TERMINAL_PROMPT=0 git -c "credential.helper=store --file=$CRED_FILE" \
        ls-remote "$REPO_URL" HEAD >/dev/null 2>&1 \
        || { rm -f "$CRED_FILE"
             die "токен не подошёл. Проверьте, что у него есть доступ к kraken-trade-bot (Contents: Read-only)"; }
    ok "токен принят"
fi

GIT_AUTH=()
[ -f "$CRED_FILE" ] && GIT_AUTH=(-c "credential.helper=store --file=$CRED_FILE")

# ── Код ──────────────────────────────────────────────────────────────────────
# Клонируем ЦЕЛИКОМ, без разреженной выкладки. Весь репозиторий — пара
# мегабайт, а разреженный список пришлось бы держать в двух местах: стоит
# добавить новый файл в корень и забыть про список — и на сервере его молча
# не окажется. Такую ошибку видно не при установке, а при первом запуске.
if [ -d "$DIR/.git" ]; then
    info "папка уже существует — обновляю код"
    git "${GIT_AUTH[@]}" -C "$DIR" fetch --quiet origin "$BRANCH" \
        || die "не удалось получить обновления"
    # Только перемотка вперёд: если на сервере оказались локальные правки,
    # затирать их молча нельзя.
    git -C "$DIR" merge --ff-only "origin/$BRANCH" --quiet \
        || die "на сервере есть локальные изменения кода. Уберите их (git -C $DIR status) и запустите снова"
    ok "код обновлён до $(git -C "$DIR" rev-parse --short HEAD)"
elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null | grep -v '^bot_data$' || true)" ]; then
    die "$DIR не пуст и не является репозиторием. Укажите другой каталог или уберите этот"
else
    info "скачиваю код с GitHub..."
    # bot_data мог быть создан выше ради токена — клонировать в непустую
    # папку git не станет, поэтому клонируем рядом и переносим.
    TMP="$(mktemp -d)"
    git "${GIT_AUTH[@]}" clone --quiet --branch "$BRANCH" "$REPO_URL" "$TMP/repo" \
        || die "не удалось склонировать репозиторий"
    mkdir -p "$DIR"
    # shellcheck disable=SC2086
    ( shopt -s dotglob; mv "$TMP/repo"/* "$DIR"/ )
    rm -rf "$TMP"
    ok "код скачан, версия $(git -C "$DIR" rev-parse --short HEAD)"
fi

# Токен нужен и кнопке «Обновить» на дашборде: она делает обычный git fetch
# из того же каталога и без сохранённого доступа упрётся в ту же стену.
if [ -f "$CRED_FILE" ]; then
    git -C "$DIR" config credential.helper "store --file=$CRED_FILE"
    ok "доступ сохранён — кнопка «Обновить» на дашборде будет работать"
fi

# ── Дальше — обычный установщик ──────────────────────────────────────────────
cd "$DIR"
chmod +x install.sh run.sh install_service.sh update.sh bootstrap.sh 2>/dev/null || true

echo
echo "── Установка окружения ─────────────────────────────────────────────────"
echo
exec ./install.sh
