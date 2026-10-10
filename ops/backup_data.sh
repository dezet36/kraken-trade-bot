#!/bin/bash
# Ежедневная копия данных бота (10.10.2026).
#
# ЗАЧЕМ. Учёт тестовых счетов живёт в одном файле — paper_state.json (балансы,
# открытые позиции, заявки, кулдауны), журналы сделок и сырые данные бирж
# (стакан, лента, ликвидации, часовые данные тетради) пишутся только у нас и
# с биржи заново не скачиваются. Состояние пишется атомарно (временный файл и
# замена), но порча диска, неудачная правка или ошибка в коде затёрли бы его
# без возврата: копий не было ни одной.
#
# ЧТО КОПИРУЕТСЯ. Весь каталог данных, КРОМЕ: моделей (16 ГБ, есть в сети),
# журнала работы bot_log.txt (его ротирует бот), ключей (.env, secrets/ — копия
# ключей в архиве — лишнее место их утечки) и временных файлов.
#
# КУДА. /opt/kraken/backups/bot_data-ГГГГ-ММ-ДД.tar.gz, хранится KEEP_DAYS дней.
# Тот же диск: защищает от порчи файлов и ошибок, но не от потери диска — для
# этого архив стоит забирать с сервера (scp) к себе.
#
# ВОССТАНОВЛЕНИЕ (остановив бота):
#   systemctl stop kraken-bot
#   tar -xzf /opt/kraken/backups/bot_data-ДАТА.tar.gz -C /opt/kraken/ bot_data/paper_state.json
#   systemctl start kraken-bot
#
# Ставится в cron root:  15 3 * * * bash /opt/kraken/code/ops/backup_data.sh
set -u
DATA=${BOT_DATA_DIR:-/opt/kraken/bot_data}
DEST=${BACKUP_DIR:-/opt/kraken/backups}
KEEP_DAYS=${KEEP_DAYS:-30}
LOG="$DEST/backup.log"
mkdir -p "$DEST"
chmod 700 "$DEST"
STAMP=$(date -u +%Y-%m-%d)
OUT="$DEST/bot_data-$STAMP.tar.gz"
TMP="$OUT.tmp"

tar -czf "$TMP" -C "$(dirname "$DATA")" \
    --exclude='bot_data/models' --exclude='bot_data/secrets' \
    --exclude='bot_data/.env*' --exclude='bot_data/bot_log.txt*' \
    --exclude='*.tmp' \
    "$(basename "$DATA")" 2>>"$LOG"
status=$?
# tar 1 — «файл изменился во время чтения» (журналы дописываются): архив годен.
if [ $status -gt 1 ] || ! tar -tzf "$TMP" >/dev/null 2>&1; then
    echo "$(date -u '+%F %T') ОШИБКА: архив не собран (tar $status)" >>"$LOG"
    rm -f "$TMP"
    exit 1
fi
if ! tar -xzOf "$TMP" "$(basename "$DATA")/paper_state.json" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>>"$LOG"; then
    echo "$(date -u '+%F %T') ОШИБКА: paper_state.json в архиве не читается" >>"$LOG"
    rm -f "$TMP"
    exit 1
fi
mv -f "$TMP" "$OUT"
chmod 600 "$OUT"
find "$DEST" -name 'bot_data-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -u '+%F %T') ок: $OUT $(du -h "$OUT" | cut -f1)" >>"$LOG"
