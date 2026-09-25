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
#   2) модель свободна (спрашиваем её саму, /slots) либо занята разбором,
#      начатым меньше GRACE секунд назад — тогда теряем только эти секунды.
#
# 23.09.2026: условие «в полёте ничего нет» без пункта 2 не наступало никогда —
# разборы идут встык, и скрипт час ждал, а потом рубил по пределу (XLM 06:02,
# пять минут счёта в корзину). Отсюда GRACE. С 25.09 занятость — по /slots, а
# не по журналу: строка «отдал модели» есть и у отказов кода без модели («нет
# законного плана»), и скрипт ждал бы вердикта, которого не будет.
LOG=/opt/kraken/bot_data/deferred_restart.log
GRACE=240                      # секунд: столько не жалко потерять
SLOTS=http://127.0.0.1:8788/slots
cd /opt/kraken/code && git pull -q --ff-only && \
  echo "$(date -u +%FT%TZ) код обновлён: $(git log --oneline -1)" >> "$LOG"

stamp_to_epoch() { date -d "$1" +%s 2>/dev/null || echo 0; }
idle() { ! curl -s -m 5 "$SLOTS" | grep -q '"is_processing":true'; }

deadline=$(( $(date +%s) + 3600 ))
while [ $(date +%s) -lt $deadline ]; do
  J=$(journalctl -u kraken-bot --no-pager -o cat --since "-3h")
  verdict=$(echo "$J" | grep -E "модель: [0-9]+ вход" | tail -1 | cut -c2-20)
  handed=$(echo "$J"  | grep -E "отдал модели (на разбор|на переделку)" | tail -1 | cut -c2-20)
  cycle=$(echo "$J"   | grep -E "LLM: сетапов найдено" | tail -1 | cut -c2-20)

  collected=1
  if [ -n "$verdict" ] && ! [[ "$cycle" > "$verdict" ]]; then collected=0; fi

  # Между фазами одного разбора (прогрев, мысль, ответ, переделка) слот
  # свободен миллисекунды: «свободна» — только если свободна дважды через 3 с.
  in_flight=0
  why="модель свободна"
  if ! { idle && sleep 3 && idle; }; then
    in_flight=1
    age=$(( $(date +%s) - $(stamp_to_epoch "$handed") ))
    if [ -n "$handed" ] && [ "$age" -le "$GRACE" ]; then
      in_flight=0
      why="начатый разбор $handed моложе ${GRACE}с"
    fi
  fi

  if [ $collected -eq 1 ] && [ $in_flight -eq 0 ]; then
    systemctl restart kraken-bot
    echo "$(date -u +%FT%TZ) вердикт $verdict забран циклом $cycle; $why — перезапущен: $(systemctl is-active kraken-bot)" >> "$LOG"
    exit 0
  fi
  sleep 10
done
systemctl restart kraken-bot
echo "$(date -u +%FT%TZ) предел ожидания — перезапущен" >> "$LOG"
