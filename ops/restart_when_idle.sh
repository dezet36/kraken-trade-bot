#!/usr/bin/env bash
# Выкатка без потери ГОТОВОГО вердикта: git pull, затем перезапуск в момент,
# когда терять нечего.
#
# Что охраняем: вердикт модели, который уже посчитан, но ещё не забран циклом.
# Такой перезапуск стоит 15–25 минут работы модели и готовый план (SUI 05:41,
# 21.09.2026). Разбор, который только начался, — это лишь потраченное время.
#
# Условия перезапуска (оба):
#   1) последний вердикт уже забран циклом («LLM: сетапов найдено» позже
#      строки «модель: N вход»);
#   2) разбор в полёте либо отсутствует, либо начался меньше GRACE минут назад
#      — тогда теряем только эти минуты.
#
# 23.09.2026: условие «в полёте ничего нет» без пункта 2 не наступало никогда —
# разборы идут встык, и скрипт час ждал, а потом рубил по пределу (XLM 06:02,
# пять минут счёта в корзину). Отсюда GRACE.
LOG=/opt/kraken/bot_data/deferred_restart.log
GRACE=240                      # секунд: столько не жалко потерять
cd /opt/kraken/code && git pull -q --ff-only && \
  echo "$(date -u +%FT%TZ) код обновлён: $(git log --oneline -1)" >> "$LOG"

stamp_to_epoch() { date -d "$1" +%s 2>/dev/null || echo 0; }

deadline=$(( $(date +%s) + 3600 ))
while [ $(date +%s) -lt $deadline ]; do
  J=$(journalctl -u kraken-bot --no-pager -o cat --since "-3h")
  verdict=$(echo "$J" | grep -E "модель: [0-9]+ вход" | tail -1 | cut -c2-20)
  # Переделка — второй ask() внутри одного decide(), своей строки
  # "отдал модели на разбор" у неё нет; без строки "отдал модели на
  # переделку" эта проверка не видела её как разбор в полёте — 24.09.2026,
  # семь оборванных запросов подряд у llm_guard.sh (та же проверка,
  # см. ops/llm_guard.sh), "Remote end closed connection". У этого скрипта
  # то же самое, просто он срабатывает не каждые 30 минут, а раз на выкатку.
  handed=$(echo "$J"  | grep -E "отдал модели (на разбор|на переделку)" | tail -1 | cut -c2-20)
  cycle=$(echo "$J"   | grep -E "LLM: сетапов найдено" | tail -1 | cut -c2-20)

  collected=1
  if [ -n "$verdict" ] && ! [[ "$cycle" > "$verdict" ]]; then collected=0; fi

  in_flight=0
  if [ -n "$handed" ] && { [ -z "$verdict" ] || [[ "$handed" > "$verdict" ]]; }; then
    age=$(( $(date +%s) - $(stamp_to_epoch "$handed") ))
    if [ "$age" -gt "$GRACE" ]; then in_flight=1; fi
  fi

  if [ $collected -eq 1 ] && [ $in_flight -eq 0 ]; then
    systemctl restart kraken-bot
    if [ -z "$handed" ] || [[ "$verdict" > "$handed" ]]; then
      why="разбора в полёте нет"
    else
      why="начатый разбор $handed моложе ${GRACE}с"
    fi
    echo "$(date -u +%FT%TZ) вердикт $verdict забран циклом $cycle; $why — перезапущен: $(systemctl is-active kraken-bot)" >> "$LOG"
    exit 0
  fi
  sleep 10
done
systemctl restart kraken-bot
echo "$(date -u +%FT%TZ) предел ожидания — перезапущен" >> "$LOG"
