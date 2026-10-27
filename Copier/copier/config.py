"""
Настройки копировщика. Читаются из COPIER_DATA_DIR/.env, затем из окружения.

Свои имена с префиксом COPIER_ — чтобы ни одна переменная торгового бота
не могла случайно повлиять на копировщик и наоборот.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

DATA_DIR = os.getenv('COPIER_DATA_DIR') or os.path.join(_ROOT, 'data')


def _load_env_file(path):
    """Простой разбор KEY=VALUE: без зависимостей, окружение главнее файла."""
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_env_file(os.path.join(DATA_DIR, '.env'))


def _float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return int(default)


def _ids(name):
    out = set()
    for part in (os.getenv(name) or '').replace(';', ',').split(','):
        part = part.strip()
        if part.lstrip('-').isdigit():
            out.add(int(part))
    return out


DB_PATH = os.getenv('COPIER_DB') or os.path.join(DATA_DIR, 'copier.db')
LOG_PATH = os.path.join(DATA_DIR, 'copier_log.txt')

# Источник сделок (решение владельца 27.10.2026): книга торгового счёта бота,
# только чтение по HTTP на localhost. Счёт по умолчанию — `upscale`; меняется в
# админке («Источник»). Импортов из бота и чтения его файлов нет.
SOURCE_URL = os.getenv('COPIER_SOURCE_URL', 'http://127.0.0.1:8787/api/accounts/feed')
SOURCE_ACCOUNT = os.getenv('COPIER_SOURCE_ACCOUNT', 'upscale')
# Адрес молчит дольше этого (бот перезапускается 1–2 мин) — новых входов нет.
SOURCE_STALE_S = _float('COPIER_SOURCE_STALE_S', 180)

# Telegram участников — свой бот, свой токен. Токен торгового бота сюда
# ставить нельзя: второй опросчик того же токена ломает оба (HTTP 409).
TG_TOKEN = os.getenv('COPIER_TG_TOKEN') or ''
ADMIN_IDS = _ids('COPIER_ADMIN_IDS')

# Ссылка регистрации на Upscale, которую бот даёт новому пользователю на /start.
SIGNUP_URL = os.getenv('COPIER_SIGNUP_URL') or 'https://app.upscale.trade'

# Мастер-ключ шифрования ключей участников (Fernet). Потерян — все ключи
# нечитаемы, участникам придётся подключиться заново.
SECRET_KEY = os.getenv('COPIER_SECRET_KEY') or ''

# Админка: только 127.0.0.1, доступ по токену (окно CopierAdmin через ssh-туннель).
ADMIN_HOST = os.getenv('COPIER_ADMIN_HOST', '127.0.0.1')
ADMIN_PORT = _int('COPIER_ADMIN_PORT', 8790)
ADMIN_TOKEN = os.getenv('COPIER_ADMIN_TOKEN') or ''

UPSCALE_URL = os.getenv('COPIER_UPSCALE_URL', 'https://api.upscale.trade').rstrip('/')

# Опрос книги счёта: заявку мастера участники получают через секунды (решение
# владельца 27.10.2026 — «с минимальной задержкой»). Запрос на localhost, без ключей.
FEED_POLL_S = _float('COPIER_FEED_POLL_S', 3)
# Позиция мастера исчезла, а строки о закрытии нет — сколько ждать до тревоги.
FEED_CLOSE_WAIT_MIN = _float('COPIER_FEED_CLOSE_WAIT_MIN', 15)
# Разом исчезло столько записей (и больше половины) без закрытий — заморозка.
FEED_MASS_VANISH = _int('COPIER_FEED_MASS_VANISH', 3)

# Сверка со счетами Upscale и снимки эквити
RECONCILE_S = _float('COPIER_RECONCILE_S', 10)
EQUITY_SNAPSHOT_MIN = _float('COPIER_EQUITY_SNAPSHOT_MIN', 5)

# Запас до лимитов просадки пропа, % от номинала счёта.
DD_BUFFER_PCT = _float('COPIER_DD_BUFFER_PCT', 0.5)
# Плечо, если рынок его не назвал (крипта на Upscale — до x5).
DEFAULT_MAX_LEVERAGE = _float('COPIER_DEFAULT_MAX_LEVERAGE', 5)

# Подтверждения в Telegram живут столько секунд (как в боте: CONFIRM_TTL_S).
CONFIRM_TTL_S = _int('COPIER_CONFIRM_TTL_S', 600)


def check():
    """Что не задано из обязательного — список строк для журнала при старте."""
    problems = []
    if not TG_TOKEN:
        problems.append('COPIER_TG_TOKEN не задан — Telegram участников выключен')
    if not ADMIN_IDS:
        problems.append('COPIER_ADMIN_IDS не задан — некому одобрять участников')
    if not SECRET_KEY:
        problems.append('COPIER_SECRET_KEY не задан — ключи участников негде хранить')
    if not ADMIN_TOKEN:
        problems.append('COPIER_ADMIN_TOKEN не задан — админка закрыта')
    return problems
