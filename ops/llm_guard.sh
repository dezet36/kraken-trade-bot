#!/bin/bash
# Сторож памяти llama-server: перезапускает модель, пока она не начала
# дочитывать собственные веса с диска.
#
# ЗАЧЕМ. Веса (15.9 ГБ) загружены через mmap и лежат в страничном кэше.
# Анонимная память самого llama-server РАСТЁТ со временем и вытесняет их.
# Замер 24.09.2026:
#
#   сразу после старта   RssAnon 0.74 ГБ   RssFile 15.93 ГБ   чтение 108 бл/с
#   через 21 час         RssAnon 8.80 ГБ   RssFile  9.14 ГБ   чтение 55000 бл/с
#
# То есть за сутки из памяти вылетело 6.8 ГБ весов, и модель стала читать их
# с диска на каждом токене: скорость упала с 2.0-2.6 до 1.35 ток/с, разбор
# вырос с 21 до 34 минут, iowait с 0% до 10%. Рост примерно 0.4 ГБ в час.
#
# ПОРОГ. Весам нужно 15.93 ГБ, всего в машине ~19.5 ГБ пригодных. Значит
# анонимная память обязана оставаться ниже примерно 3 ГБ, иначе вытеснение
# начинается. Берём 2.5 ГБ — это около шести часов работы.
#
# КОГДА МОЖНО. Перезапуск в середине разбора стоит получаса машинного
# времени, поэтому условие то же, что у restart_when_idle.sh: последний
# вердикт забран циклом, а начатый разбор либо отсутствует, либо моложе
# GRACE секунд. Не дождались за час — не трогаем вовсе: сторож приходит
# снова через полчаса, спешить некуда.
LOG=/opt/kraken/bot_data/llm_guard.log
LIMIT_GB=2.5
GRACE=240
WAIT=3600

P=$(pgrep -f llama-server | head -1)
[ -z "$P" ] && exit 0

anon_gb=$(awk '/^RssAnon/{printf "%.2f", $2/1048576}' "/proc/$P/status" 2>/dev/null)
[ -z "$anon_gb" ] && exit 0
over=$(awk -v a="$anon_gb" -v l="$LIMIT_GB" 'BEGIN{print (a>l)?1:0}')
[ "$over" -eq 0 ] && exit 0

stamp_to_epoch() { date -d "$1" +%s 2>/dev/null || echo 0; }

deadline=$(( $(date +%s) + WAIT ))
while [ $(date +%s) -lt $deadline ]; do
  J=$(journalctl -u kraken-bot --no-pager -o cat --since "-3h")
  verdict=$(echo "$J" | grep -E "модель: [0-9]+ вход" | tail -1 | cut -c2-20)
  # Переделка — второй ask() внутри одного decide(), своей строки
  # "отдал модели на разбор" у неё нет; без строки "отдал модели на
  # переделку" эта проверка не видела её как разбор в полёте — 24.09.2026,
  # семь оборванных запросов подряд, "Remote end closed connection".
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
    systemctl restart kraken-llm
    sleep 60
    Q=$(pgrep -f llama-server | head -1)
    now=$(awk '/^RssAnon/{printf "%.2f", $2/1048576}' "/proc/$Q/status" 2>/dev/null)
    res=$(awk '/^RssFile/{printf "%.2f", $2/1048576}' "/proc/$Q/status" 2>/dev/null)
    echo "$(date -u +%FT%TZ) анонимная память ${anon_gb} ГБ > ${LIMIT_GB} — модель перезапущена; стало anon ${now} ГБ, весов в памяти ${res} ГБ" >> "$LOG"
    exit 0
  fi
  sleep 30
done
echo "$(date -u +%FT%TZ) анонимная память ${anon_gb} ГБ > ${LIMIT_GB}, но разбор шёл весь час — отложено до следующего захода" >> "$LOG"
