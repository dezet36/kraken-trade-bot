# Kraken trading bot — образ содержит ТОЛЬКО зависимости.
# Код и состояние (Live_Bot/) бинд-монтируются томом из docker-compose.yml:
# обновление бота = git pull на хосте + docker compose restart (без пересборки),
# все state-файлы/журналы/БД живут на хосте и переживают пересоздание контейнера.
FROM python:3.13-slim

# Пакеты для сборки колёс, если под slim не найдётся готовых (numpy/pandas/matplotlib)
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Непривилегированный пользователь (том монтируется с правами хоста)
RUN useradd -m botuser
USER botuser

# Команда задаётся в docker-compose.yml (bot.py или platform_bot.py)
CMD ["python", "bot.py"]
