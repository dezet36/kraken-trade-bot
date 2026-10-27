#!/usr/bin/env bash
# С машины разработчика (Git Bash): залить Copier/ на сервер и выкатить — без git push.
#
#   export COPIER_SSH='ssh -i ~/.ssh/kraken -o IdentitiesOnly=yes -p <порт> root@<сервер>'
#   bash Copier/ops/upload.sh            — обновить работающий копировщик (тесты до замены)
#   bash Copier/ops/upload.sh --only     — только залить в /opt/copier/code.new (для первой установки)
#
# Адрес сервера в файл не записан: репозиторий публичный.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
[ -n "${COPIER_SSH:-}" ] || { echo "задайте COPIER_SSH (команда ssh до сервера)"; exit 1; }

tar -C "$ROOT" -czf - \
    --exclude='Copier/data' --exclude='Copier/.probe_key' --exclude='Copier/probe_results' \
    --exclude='Copier/desktop/build' --exclude='Copier/desktop/dist' --exclude='*.spec' \
    --exclude='__pycache__' --exclude='.pytest_cache' --exclude='copier_admin*' \
    Copier \
  | $COPIER_SSH 'rm -rf /opt/copier/code.new && mkdir -p /opt/copier/code.new && tar -xzf - -C /opt/copier/code.new'
echo "залито в /opt/copier/code.new"
if [ "${1:-}" != "--only" ]; then
  $COPIER_SSH 'bash /opt/copier/code.new/Copier/ops/deploy.sh --upload'
fi
