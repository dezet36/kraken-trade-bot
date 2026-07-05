# Деплой Kraken-бота на сервер (Docker)

Бот упакован в контейнер: свои зависимости, без открытых портов (только исходящие
HTTPS к Bybit и Telegram), лимиты CPU/RAM. Другие боты на том же сервере ему не
мешают и он не мешает им — при одном условии: **у каждого бота свой Telegram-токен**.

## Установка (один раз)

```bash
# 1. Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com | sh

# 2. Код бота
git clone https://github.com/dezet36/kraken-trade-bot.git
cd kraken-trade-bot

# 3. Секреты: скопируй свой Live_Bot/.env на сервер (в репозитории его НЕТ)
#    scp "d:\Bot trade\Live_Bot\.env" user@server:~/kraken-trade-bot/Live_Bot/.env
chmod +x start_server.sh stop_server.sh
```

## Запуск / остановка / обновление

```bash
./start_server.sh                      # запуск (личная торговля, bot.py)
docker compose logs -f kraken-trader   # живые логи
./stop_server.sh                       # остановка (state сохраняется)

git pull && docker compose restart kraken-trader   # обновление кода
```

Мульти-юзер платформа вместо личного бота:
```bash
docker compose --profile platform up -d kraken-platform
```

## ВАЖНО: правила бесконфликтности

1. **Не запускай `kraken-trader` и `kraken-platform` одновременно** — оба поллят
   один TELEGRAM_BOT_TOKEN, Telegram вернёт 409-конфликт. Один токен = один процесс.
2. **Второй (чужой) бот на сервере** — просто в своей папке со своим compose и
   СВОИМ телеграм-токеном. Портов наш бот не занимает, имена контейнеров уникальны
   (`kraken-trader`), сеть изолирована compose-проектом.
3. **Состояние на хосте**: `Live_Bot/` бинд-монтируется в контейнер целиком —
   `state/`, `platform.db`, `trades_detail.jsonl`, `bot_log.txt`, `.env` живут на
   сервере и переживают пересоздание контейнера. Бэкап = копия папки `Live_Bot/`.
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
