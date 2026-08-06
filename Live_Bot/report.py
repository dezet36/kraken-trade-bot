"""
Отчёт для разбора неполадок: один файл, который можно отправить целиком.

ЗАЧЕМ. Когда что-то ломается, вопрос «а что там в логе» превращается в
переписку из десяти сообщений: пришлите ошибку, пришлите ещё строк выше,
а какая версия, а режим какой, а настройки не меняли? Всё это известно
приложению. Пусть оно и соберёт.

ЧТО ВНУТРИ. Версия и режим, настройки, сгруппированные ошибки с
трассировками, хвост журнала, короткая сводка состояния. Ровно то, с чего
начинается любой разбор.

ЧЕГО ВНУТРИ НЕТ, И ЭТО ГЛАВНОЕ. Ключей. Отчёт делается для того, чтобы его
ОТПРАВИТЬ, и это меняет цену ошибки: секрет, попавший в такой файл, уезжает
вместе с ним и может остаться в переписке, в почте, в чужом кэше навсегда.
Поэтому чистка устроена в три слоя, и все три работают одновременно:

    по имени      значения переменных окружения, чьё имя похоже на секрет
                  (KEY, SECRET, TOKEN, PASSWORD, WEBHOOK), заменяются
                  целиком, где бы они в тексте ни встретились;
    по форме      известные форматы секретов — токены GitHub, ключи бирж,
                  токены Telegram — вырезаются по образцу, даже если такой
                  переменной у нас нет и значение попало из чужого текста;
    по длине      длинные бессмысленные строки из букв и цифр вырезаются
                  как подозрительные, даже если формат неизвестен.

Третий слой намеренно перестраховывается: он может вырезать безобидный
идентификатор. Это правильный размен — потерянный в отчёте хэш стоит одного
уточняющего вопроса, потерянный ключ стоит денег.

ОГОВОРКА, КОТОРУЮ ВИДНО В САМОМ ФАЙЛЕ. Автоматическая чистка не бывает
полной. В шапке отчёта стоит просьба пробежать файл глазами перед отправкой.
"""

import io
import os
import platform
import re
import sys
from datetime import datetime, timezone

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Имена переменных, значения которых секретны. Сравнение по вхождению:
# BYBIT_API_KEY, GITHUB_TOKEN, TELEGRAM_BOT_TOKEN попадают все.
SECRET_NAME_PARTS = ('KEY', 'SECRET', 'TOKEN', 'PASSWORD', 'PASSWD', 'PWD',
                     'WEBHOOK', 'CREDENTIAL', 'PRIVATE')

# Имена, которые содержат эти части, но секретами НЕ являются. Без списка
# исключений вырезалось бы и то, что нужно для разбора.
SECRET_NAME_ALLOW = ('API_KEY_SET', 'HAS_KEY', 'KEYBOARD')

MASK = '⟨вырезано⟩'

# Известные форматы. Порядок важен: сначала длинные и специфичные.
PATTERNS = (
    # GitHub: классические и тонкой настройки
    re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),
    # Telegram: 8-10 цифр, двоеточие, 35 символов
    re.compile(r'\b\d{8,10}:[A-Za-z0-9_-]{30,}'),
    # Заголовок авторизации
    re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}'),
    # Ключ=значение прямо в тексте: api_key=..., secret: "..."
    re.compile(r'(?i)\b(\w*(?:key|secret|token|password)\w*)'
               r'\s*[:=]\s*["\']?([A-Za-z0-9._~+/=-]{12,})["\']?'),
)

# Длинная бессмысленная строка: не меньше 24 символов, есть и буквы, и цифры.
# Слова русского и английского языка сюда не попадают — в них нет цифр.
_LONG_TOKEN = re.compile(r'\b(?=[A-Za-z0-9_-]{24,}\b)'
                         r'(?=[A-Za-z0-9_-]*[A-Za-z])'
                         r'(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{24,}\b')

# Строки, похожие на длинный токен, но нужные для разбора. Без этого
# вырезались бы имена файлов сборки и подписи коммитов, по которым как раз
# и опознаётся версия.
_LONG_ALLOW = re.compile(r'(?i)^(?:[0-9a-f]{7,12}|v?\d+[\d.]*|'
                         r'[A-Z0-9]{2,12}(?:USDT|USD|PERP))$')


def _secret_values():
    """Значения секретных переменных окружения — их и вырезаем по тексту."""
    out = []
    for name, value in os.environ.items():
        upper = name.upper()
        if any(ok in upper for ok in SECRET_NAME_ALLOW):
            continue
        if not any(part in upper for part in SECRET_NAME_PARTS):
            continue
        value = (value or '').strip()
        # Совсем короткие не вырезаем: значение вроде «true» или «1» встретится
        # в тексте сто раз и превратит отчёт в решето из масок.
        if len(value) >= 8:
            out.append(value)
    # Длинные первыми: иначе короткий секрет, оказавшийся куском длинного,
    # разрежет его пополам и оставит хвост в открытом виде.
    return sorted(set(out), key=len, reverse=True)


def scrub(text):
    """
    Чистит текст от секретов. Три слоя, описанные в шапке модуля.

    Никогда не бросает: отчёт без части содержимого лучше, чем отсутствие
    отчёта, а вот отчёт без чистки — хуже обоих.
    """
    if not text:
        return ''
    try:
        for value in _secret_values():
            text = text.replace(value, MASK)
        for pattern in PATTERNS:
            if pattern.groups >= 2:
                text = pattern.sub(lambda m: f'{m.group(1)}={MASK}', text)
            else:
                text = pattern.sub(MASK, text)
        text = _LONG_TOKEN.sub(
            lambda m: m.group(0) if _LONG_ALLOW.match(m.group(0)) else MASK,
            text)
        return text
    except Exception as exc:                       # noqa: BLE001
        # Если чистка сломалась, отдавать текст НЕЛЬЗЯ.
        return f'{MASK} (чистка не отработала: {exc})'


def scrub_obj(value):
    """
    Чистит СТРОКИ внутри структуры, не трогая саму структуру.

    Так и надо чистить всё, что потом снова станет JSON. Прогнать через
    scrub() готовый JSON-текст нельзя: правило «ключ=значение» поймает в нём
    пару вида "key": "abc…" и заменит её на key=⟨вырезано⟩ — вместе с
    кавычками и двоеточием. Структура развалится, разбор упадёт, и вместо
    списка ошибок пользователь увидит пятисотую. Наступил на это ровно один
    раз, поэтому здесь отдельная функция и эта запись.
    """
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_obj(v) for v in value]
    return value


def _version():
    """
    Версия — из того же места, откуда её берёт обновление.

    Своя копия чтения файла VERSION здесь стояла ровно один раз и уже была
    неверной: она не знала про sys._MEIPASS, то есть в собранном приложении
    искала файл не там и всегда отвечала «неизвестна». А отчёт без версии —
    это первый же уточняющий вопрос в переписке.
    """
    try:
        import updater_app
        version = updater_app.current_version()
        if version:
            return version
        return 'сборка «из рук», файла VERSION нет'
    except Exception as exc:                       # noqa: BLE001
        return f'не определилась: {exc}'


def _head():
    import config

    mode = getattr(config, 'TRADING_MODE', '?')
    lines = [
        'ОТЧЁТ ДЛЯ РАЗБОРА НЕПОЛАДОК',
        '',
        'Ключи и токены из этого файла вырезаны автоматически. Чистка не',
        'бывает полной — пробегите файл глазами перед отправкой. Если увидите',
        f'что-то похожее на ключ, замените на {MASK} вручную.',
        '',
        f'собран:      {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC',
        f'версия:      {_version()}',
        f'режим:       {mode}',
        f'биржа:       {getattr(config, "EXCHANGE_NAME", "?")}',
        f'стратегии:   {getattr(config, "STRATEGY", "?")}',
        f'система:     {platform.system()} {platform.release()}',
        f'python:      {sys.version.split()[0]}',
        f'запуск:      {"собранное приложение" if getattr(sys, "frozen", False) else "из исходников"}',
    ]
    return '\n'.join(lines)


def _section(title, body):
    rule = '─' * 74
    return f'\n{rule}\n{title}\n{rule}\n{body.rstrip() or "— пусто —"}\n'


def _errors():
    try:
        import error_log
    except Exception as exc:                       # noqa: BLE001
        return f'журнал ошибок недоступен: {exc}'

    groups = error_log.snapshot(limit=60)
    if not groups:
        return '— ошибок не записано —'
    out = io.StringIO()
    summary = error_log.summary()
    out.write(f'групп {summary.get("groups", 0)}, '
              f'случаев всего {summary.get("total", 0)}\n')
    for i, group in enumerate(groups, 1):
        out.write(f'\n[{i}] {group.get("category", "прочее")} · '
                  f'{group.get("level", "ERROR")} · '
                  f'повторов {group.get("count", 0)} · '
                  f'с {group.get("first", "?")} по {group.get("last", "?")}\n')
        for sample in group.get('samples', [])[:2]:
            out.write(f'    {sample.get("at", "?")}  {sample.get("text", "")}\n')
            context = sample.get('context') or {}
            if context:
                pairs = ', '.join(f'{k}={v}' for k, v in context.items())
                out.write(f'    контекст: {pairs}\n')
            trace = sample.get('traceback')
            if trace:
                for line in trace.strip().splitlines():
                    out.write(f'    │ {line}\n')
    return out.getvalue()


def _settings():
    try:
        import json

        import settings_store as settings
        return json.dumps(settings.load(), ensure_ascii=False, indent=2)
    except Exception as exc:                       # noqa: BLE001
        return f'настройки недоступны: {exc}'


def _state():
    """Короткая сводка состояния — без списка сделок целиком."""
    try:
        import dashboard
        payload = dashboard.build_payload()
    except Exception as exc:                       # noqa: BLE001
        return f'состояние недоступно: {exc}'

    closed = payload.get('closed') or []
    open_positions = payload.get('open_positions') or []
    pending = payload.get('pending') or []
    lines = [
        f'открытых позиций: {len(open_positions)}',
        f'ожидающих ордеров: {len(pending)}',
        f'закрытых сделок в журнале: {len(closed)}',
    ]
    for name, item in (payload.get('strategies') or {}).items():
        lines.append(f'  {name}: сделок {item.get("trades", 0)}, '
                     f'винрейт {item.get("winrate", 0)}%, '
                     f'итог {item.get("pnl", 0)}')
    status = payload.get('status') or {}
    if status:
        lines.append(f'состояние бота: {status}')
    # Последние пять сделок: по ним видно, торгует ли он вообще и чем.
    if closed:
        lines.append('')
        lines.append('последние сделки:')
        for trade in closed[:5]:
            lines.append(f'  {trade.get("closed", "?")}  {trade.get("strategy")}  '
                         f'{trade.get("pair")}  {trade.get("direction")}  '
                         f'итог {trade.get("pnl")}  причина {trade.get("reason")}')
    return '\n'.join(lines)


def _log(limit=400):
    try:
        import dashboard
        return '\n'.join(dashboard.read_log(limit=limit))
    except Exception as exc:                       # noqa: BLE001
        return f'журнал недоступен: {exc}'


def build(log_lines=400):
    """
    Готовый отчёт одной строкой. Не бросает: неполный отчёт полезнее пустого.

    Чистка применяется к ВСЕМУ тексту разом, в самом конце, а не к каждому
    куску по отдельности. Так секрет не проскочит через раздел, который
    забыли почистить: пропустить кусок мимо чистки просто невозможно.
    """
    parts = [_head()]
    for title, builder in (
        ('ОШИБКИ', _errors),
        ('СОСТОЯНИЕ', _state),
        ('НАСТРОЙКИ', _settings),
        (f'ЖУРНАЛ · последние {log_lines} строк', lambda: _log(log_lines)),
    ):
        try:
            parts.append(_section(title, builder()))
        except Exception as exc:                   # noqa: BLE001
            parts.append(_section(title, f'раздел не собрался: {exc}'))
    return scrub('\n'.join(parts))


def filename():
    return f'kraken-отчёт-{datetime.now():%Y%m%d-%H%M}.txt'
