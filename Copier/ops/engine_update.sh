#!/usr/bin/env bash
# Перевод движка участников на новую версию стратегий — ТОЛЬКО рукой владельца.
#   bash engine_update.sh [коммит] [--with-settings]
# Коммит по умолчанию — тот, что торговый бот выполняет сейчас (HEAD /opt/kraken/code).
#
# 1. Новая версия собирается рядом (engine.new), тесты бота на ней прогоняются
#    целиком, включая эталоны решений (test_golden_*). Не прошли — ничего не меняется.
# 2. Новые входы участников на паузе; движок останавливается; код подменяется;
#    папка данных движка сохраняется — заявки и позиции мастера живут дальше.
# 3. --with-settings — заодно снять текущие настройки стратегий торгового бота.
# 4. Движок запускается; как только он записал состояние, пауза снимается
#    (если она не стояла до обновления). Лента копировщика сама заморозится,
#    если новая версия пишет состояние в другом формате.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/common.sh"
need_root

REF=''
WITH_SETTINGS=''
for arg in "$@"; do
  case "$arg" in
    --with-settings) WITH_SETTINGS=1 ;;
    *) REF=$arg ;;
  esac
done
SRC=$(git -C "$ENGINE" remote get-url origin)
REF=${REF:-$(git -C "$SRC" rev-parse HEAD)}
NEW=$COPIER/engine.new
NEW_VENV=$COPIER/engine_venv.new
STAMP=$(date -u +%Y%m%d%H%M%S)
OLD=$(git -C "$ENGINE" describe --tags --always)

rm -rf "$NEW" "$NEW_VENV"
git clone -q "$SRC" "$NEW"
git -C "$NEW" -c advice.detachedHead=false checkout -q --detach "$REF"
TAG=engine-$(date -u +%Y-%m-%d)-$(git -C "$NEW" rev-parse --short HEAD)
git -C "$NEW" tag -f "$TAG" >/dev/null
say "проверяю версию $TAG — $(git -C "$NEW" log --oneline -1)"

if cmp -s "$NEW/requirements.txt" "$ENGINE/requirements.txt"; then
  RUN_VENV=$ENGINE_VENV
else
  python3 -m venv "$NEW_VENV"
  "$NEW_VENV/bin/python" -m pip install -q -r "$NEW/requirements.txt"
  RUN_VENV=$NEW_VENV
fi
"$RUN_VENV/bin/python" -m pip install -q pytest >/dev/null
( cd "$NEW/Live_Bot" && BOT_DATA_DIR=$(mktemp -d) nice -n 19 "$RUN_VENV/bin/python" -m pytest -q -x tests ) \
  || die "тесты новой версии не прошли — движок не тронут"

WAS_PAUSED=0
entries_paused && WAS_PAUSED=1
admin_post /api/system '{"action":"pause_entries"}' >/dev/null || say "внимание: копировщик не ответил — пауза не поставлена"

systemctl stop copier-engine
mv "$ENGINE" "$COPIER/engine.prev.$STAMP"
mv "$NEW" "$ENGINE"
if [ "$RUN_VENV" = "$NEW_VENV" ]; then
  mv "$ENGINE_VENV" "$COPIER/engine_venv.prev.$STAMP"
  mv "$NEW_VENV" "$ENGINE_VENV"
fi
chown -R root:root "$ENGINE"
[ -n "$WITH_SETTINGS" ] && snapshot_settings
BEFORE=$(stat -c %Y "$ENGINE_DATA/paper_state.json" 2>/dev/null || echo 0)
systemctl start copier-engine

NOW=$BEFORE
for _ in $(seq 1 80); do                       # до ~13 минут: цикл движка 5 минут
  sleep 10
  NOW=$(stat -c %Y "$ENGINE_DATA/paper_state.json" 2>/dev/null || echo 0)
  [ "$NOW" -gt "$BEFORE" ] && break
done
[ "$NOW" -gt "$BEFORE" ] || die "движок не записал состояние после запуска — входы оставлены на паузе, смотрите journalctl -u copier-engine"

[ "$WAS_PAUSED" -eq 0 ] && admin_post /api/system '{"action":"resume_entries"}' >/dev/null
bash "$HERE/engine_info.sh" || true
say "движок обновлён: было [$OLD] → стало [$TAG]; прежняя версия — $COPIER/engine.prev.$STAMP"
