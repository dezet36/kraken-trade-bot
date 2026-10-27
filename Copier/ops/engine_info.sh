#!/usr/bin/env bash
# Сведения о версии движка для админки (cron раз в час, root):
# тег и коммит, дата, и что изменилось в стратегиях торгового бота после этой
# версии. git fetch обновляет только ссылки на удалённую ветку — рабочее дерево
# движка (отсоединённый HEAD на теге) не трогается.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/common.sh"
need_root

git -C "$ENGINE" fetch -q origin 2>/dev/null || true
TAG=$(git -C "$ENGINE" describe --tags --exact-match 2>/dev/null || echo "")
COMMIT=$(git -C "$ENGINE" rev-parse --short HEAD)
DATE=$(git -C "$ENGINE" log -1 --format=%cI)
CHANGES=$(git -C "$ENGINE" log --format='%h %cs %s' HEAD..origin/main -- \
    Live_Bot/strategies Live_Bot/analysis Live_Bot/execution Live_Bot/accounts/paper.py \
    2>/dev/null | head -60 || true)

TAG="$TAG" COMMIT="$COMMIT" DATE="$DATE" CHANGES="$CHANGES" OUT="$DATA/engine_info.json" \
"$VENV/bin/python" - <<'PY'
import json, os
from datetime import datetime, timezone
data = {'tag': os.environ['TAG'] or None, 'commit': os.environ['COMMIT'], 'date': os.environ['DATE'],
        'changes': [c for c in os.environ['CHANGES'].splitlines() if c.strip()],
        'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
tmp = os.environ['OUT'] + '.tmp'
with open(tmp, 'w', encoding='utf-8') as fh:
    json.dump(data, fh, ensure_ascii=False)
os.replace(tmp, os.environ['OUT'])
PY
chown copier:copier "$DATA/engine_info.json"
