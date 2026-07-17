# Деплой Kraken-бота на сервер (Docker)

Бот упакован в контейнер: свои зависимости, без открытых портов (только исходящие
HTTPS к Bybit и Telegram), лимиты CPU/RAM. Другие боты на том же сервере ему не
мешают и он не мешает им — при одном условии: **у каждого бота свой Telegram-токен**.

## Архитектура томов (важно!)

Код и данные — **два физически разных каталога на хосте**:

- `Live_Bot/` → код. Можно **безопасно перезаписывать целиком** при каждом
  обновлении — хоть `git pull`, хоть ручное копирование папки поверх старой.
  Ничего важного здесь больше не хранится.
- `bot_data/` → `.env`, журнал сделок (`trades_journal.csv`, `trades_detail.jsonl`),
  БД платформы (`platform.db`), состояние позиций/кулдаунов/pending-ордеров
  (`state/`, `cooldown_state.json`, ...), логи. Копирование/замена `Live_Bot/`
  этот каталог никак не затрагивает.

Раньше все данные лежали внутри `Live_Bot/` и **терялись при каждом полном
копировании папки поверх старой** (если копировать «всё подряд», не исключая
файлы вручную). Теперь это исключено архитектурно — исключать вручную ничего
не нужно.

## Установка (один раз)

```bash
# 1. Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh

# 2. Код бота — любым способом:
git clone https://github.com/dezet36/kraken-trade-bot.git kraken-trade-bot
#   ИЛИ просто скопируй папку Live_Bot целиком в kraken-trade-bot/Live_Bot
cd kraken-trade-bot
chmod +x start_server.sh stop_server.sh

# 3. Секреты: помести свой .env в bot_data/ (НЕ в Live_Bot/)
mkdir -p bot_data
scp "d:\Bot trade\Live_Bot\.env" user@server:~/kraken-trade-bot/bot_data/.env
```

Если у тебя уже есть старый деплой (данные ещё лежат внутри `Live_Bot/`) —
ничего переносить вручную не нужно: `start_server.sh` сделает это сам при
первом запуске (см. «Первый запуск на старой схеме» ниже).

## Запуск / остановка / обновление

```bash
./start_server.sh                      # запуск (личная торговля, bot.py)
docker compose logs -f kraken-trader   # живые логи
./stop_server.sh                       # остановка (bot_data/ сохраняется)

# Обновление кода — ЛЮБЫМ способом, папку Live_Bot/ теперь можно заменять целиком:
git pull && docker compose restart kraken-trader
#   ИЛИ: скопировать новую версию Live_Bot/ поверх старой (без исключений) и:
docker compose restart kraken-trader
```

Мульти-юзер платформа вместо личного бота:
```bash
docker compose --profile platform up -d kraken-platform
```

## Первый запуск на старой схеме (миграция)

Если раньше данные лежали в `Live_Bot/` (`.env`, `platform.db`,
`trades_journal.csv`, `state/`, ...), `start_server.sh` при первом запуске под
новой схемой **автоматически переносит** их в `bot_data/` (разово, только если
`bot_data/.env` ещё не существует). После этого `Live_Bot/` можно заменять
целиком без каких-либо исключений — `bot_data/` больше не пострадает.

## ВАЖНО: правила бесконфликтности

1. **Не запускай `kraken-trader` и `kraken-platform` одновременно** — оба поллят
   один TELEGRAM_BOT_TOKEN, Telegram вернёт 409-конфликт. Один токен = один процесс.
2. **Второй (чужой) бот на сервере** — просто в своей папке со своим compose и
   СВОИМ телеграм-токеном. Портов наш бот не занимает, имена контейнеров уникальны
   (`kraken-trader`), сеть изолирована compose-проектом.
3. **Данные на хосте — каталог `bot_data/`** (не `Live_Bot/`): `.env`, `state/`,
   `platform.db`, `trades_detail.jsonl`, `bot_log.txt` живут там и переживают
   пересоздание контейнера И полную замену папки `Live_Bot/`. Бэкап = копия
   папки `bot_data/`.
4. **Часовой пояс контейнера — UTC**: сессионный фильтр (12–16 UTC) корректен по
   построению; времена в логах/уведомлениях будут в UTC.
5. Автозапуск после ребута сервера: `restart: unless-stopped` уже в compose —
   достаточно, чтобы Docker стартовал с системой (`systemctl enable docker`).

## Проверка после запуска

```bash
docker compose ps                                  # State = running
docker compose logs --tail 50 kraken-trader        # цикл сканирует пары
# в Telegram: /status должен ответить
```
