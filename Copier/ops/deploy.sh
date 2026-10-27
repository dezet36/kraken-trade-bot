#!/usr/bin/env bash
# Обновление кода СЛУЖБЫ копировщика (не движка). Тесты — до замены; не прошли —
# служба остаётся на прежнем коде.
#   bash deploy.sh --upload        — код залит ops/upload.sh в /opt/copier/code.new
#   bash deploy.sh [ветка|коммит]  — код из git (если /opt/copier/code — клон)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/common.sh"
need_root

finish() {
  chown -R root:root "$CODE"
  install -m 644 "$CODE/Copier/ops/copier.service" /etc/systemd/system/
  install -m 644 "$CODE/Copier/ops/copier-engine.service" /etc/systemd/system/
  systemctl daemon-reload
  if grep -qE '^COPIER_TG_TOKEN=.+' "$DATA/.env" 2>/dev/null; then
    systemctl restart copier
  fi
}

run_tests() {   # run_tests <папка с Copier>
  "$VENV/bin/python" -m pip install -q -r "$1/Copier/requirements.txt" pytest
  ( cd "$1/Copier" && nice -n 19 "$VENV/bin/python" -m pytest -q -p no:cacheprovider tests )
}

if [ "${1:-}" = "--upload" ]; then
  NEW=$COPIER/code.new
  [ -d "$NEW/Copier" ] || die "нет $NEW/Copier — сначала ops/upload.sh"
  run_tests "$NEW" || die "тесты копировщика не прошли — служба на прежнем коде"
  STAMP=$(date -u +%Y%m%d%H%M%S)
  [ -d "$CODE" ] && mv "$CODE" "$COPIER/code.prev.$STAMP"
  mv "$NEW" "$CODE"
  ls -dt "$COPIER"/code.prev.* 2>/dev/null | tail -n +4 | xargs -r rm -rf
  finish
  say "служба копировщика обновлена из залитого кода (прежний — code.prev.$STAMP)"
  exit 0
fi

REF=${1:-origin/main}
OLD=$(git -C "$CODE" rev-parse HEAD)
git_c -C "$CODE" fetch -q origin
git -C "$CODE" -c advice.detachedHead=false checkout -q --detach "$REF"
if ! run_tests "$CODE"; then
  git -C "$CODE" -c advice.detachedHead=false checkout -q --detach "$OLD"
  die "тесты копировщика не прошли — откат на $OLD"
fi
finish
say "служба копировщика обновлена: $(git -C "$CODE" log --oneline -1)"
