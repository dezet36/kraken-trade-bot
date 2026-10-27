#!/usr/bin/env bash
# Резервная копия базы копировщика (cron 03:35). Ключи участников в базе
# зашифрованы; мастер-ключ (.env) в копию НЕ входит — храните его отдельно.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/common.sh"
need_root

OUT=$COPIER/backups/copier-$(date -u +%Y%m%d).db
DB="$DATA/copier.db" OUT="$OUT" "$VENV/bin/python" - <<'PY'
import os, sqlite3
src = sqlite3.connect(os.environ['DB'])
dst = sqlite3.connect(os.environ['OUT'])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
chown copier:copier "$OUT"
chmod 600 "$OUT"
find "$COPIER/backups" -name 'copier-*.db' -mtime +14 -delete
cp -p "$ENGINE_DATA/paper_trades.jsonl" "$COPIER/backups/engine_trades-$(date -u +%Y%m%d).jsonl" 2>/dev/null || true
find "$COPIER/backups" -name 'engine_trades-*.jsonl' -mtime +14 -delete
