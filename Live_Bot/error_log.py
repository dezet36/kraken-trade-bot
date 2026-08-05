"""
Журнал ошибок: что сломалось, сколько раз и когда в последний раз.

ЗАЧЕМ ОТДЕЛЬНО ОТ ЛОГА. В bot_log.txt ошибка живёт одной строкой среди
тысяч обычных, и через сутки её не найти. Хуже другое: одна и та же
проблема — недоступная биржа, занятый файл — печатается сотнями
одинаковых строк, и по логу невозможно понять, это одна беда или сто
разных. Здесь одинаковые ошибки схлопываются в одну запись со счётчиком:
видно, что именно ломается и насколько часто.

КАК СОБИРАЕТСЯ. Не расстановкой вызовов по коду — таких мест уже 69, и
новые появятся при любой правке. Модуль подключается к logger обработчиком
и забирает всё, что бот и так печатает со значками ⚠️ и ❌, а также любые
записи уровня WARN и ERROR. Дополнительно есть record() с трассировкой для
мест, где она осмысленна.

ГРУППИРОВКА. Подпись строится по «форме» сообщения: числа, цены и имена пар
заменяются на #. «BTCUSDT: таймаут 30с» и «ETHUSDT: таймаут 5с» — одна
проблема, а не две.

Файл лежит в каталоге ДАННЫХ: обновление кода его не трогает, история
ошибок переживает перезапуск и обновление.
"""

import json
import os
import re
import threading
import traceback
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv('BOT_DATA_DIR') or _BASE_DIR
ERRORS_FILE = os.path.join(DATA_DIR, 'errors.json')

# Больше этого числа групп не храним: журнал ошибок не должен сам стать
# проблемой на диске. Вытесняется та, которую дольше всего не видели.
MAX_GROUPS = 200
MAX_SAMPLES = 5

_lock = threading.Lock()
_groups = None

# Классификация по ключевым словам. Категория нужна не для красоты: сетевые
# сбои лечатся иначе, чем нехватка прав на файл, и смешивать их в одну кучу
# значит каждый раз разбираться заново.
CATEGORIES = (
    ('сеть', ('сетев', 'timeout', 'таймаут', 'networkerror', 'connection',
              'не отвечает', 'подключ', 'dns', 'ssl')),
    # Порядок важен: проверка идёт сверху вниз, и «permission denied» на
    # файле не должно попасть в биржевые ошибки только потому, что там тоже
    # встречается слово permission.
    ('данные', ('не читается', 'нечитаем', 'не удалось сохранить', 'json',
                'csv', 'файл', 'permissionerror', 'errno', 'permission denied',
                'нет данных', 'мало данных', 'диск')),
    ('биржа', ('api', 'retcode', 'ключ', 'orderbook', 'insufficient',
               'маржа', 'биржа', 'exchange', 'ордер')),
    ('стратегия', ('стратег', 'сканирован', 'сигнал', 'сетап', 'контекст')),
    ('позиции', ('позиц', 'стоп', 'тейк', 'закрыт', 'восстанов')),
)

_NUMBER = re.compile(r'\d+[\d.,:]*')
_PAIR = re.compile(r'\b[A-Z0-9]{2,12}(?:USDT|USD|PERP)\b')
_QUOTED = re.compile(r'["\'][^"\']{0,80}["\']')


def _classify(message):
    low = message.lower()
    for name, words in CATEGORIES:
        if any(word in low for word in words):
            return name
    return 'прочее'


def _signature(message):
    """
    «Форма» сообщения: одинаковые беды с разными числами — одна запись.

    Без этого недоступная на десять минут биржа даёт триста отдельных
    записей, и журнал ошибок становится таким же нечитаемым, как обычный лог.
    """
    text = _PAIR.sub('#PAIR', message)
    text = _QUOTED.sub('"…"', text)
    text = _NUMBER.sub('#', text)
    return text.strip()[:300]


def _load():
    global _groups
    if _groups is not None:
        return _groups
    try:
        with open(ERRORS_FILE, encoding='utf-8') as fh:
            data = json.load(fh)
        _groups = data if isinstance(data, dict) else {}
    except Exception:                              # noqa: BLE001
        _groups = {}
    return _groups


def _save():
    """Запись best-effort: журнал ошибок не имеет права ронять бота."""
    try:
        tmp = ERRORS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(_groups, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, ERRORS_FILE)
    except Exception:                              # noqa: BLE001
        pass


def record(message, level='ERROR', category=None, exc=None, **context):
    """
    Заносит ошибку в журнал. Никогда не бросает исключений сам.

    exc — исключение, если есть: трассировка сохраняется, по ней потом
    видно, где именно сломалось, а не только что сломалось.
    """
    try:
        message = str(message).strip()
        if not message:
            return
        with _lock:
            groups = _load()
            key = _signature(message)
            now = datetime.now().isoformat(timespec='seconds')
            item = groups.get(key)
            if item is None:
                if len(groups) >= MAX_GROUPS:
                    oldest = min(groups, key=lambda k: groups[k].get('last', ''))
                    groups.pop(oldest, None)
                item = {
                    'category': category or _classify(message),
                    'level': level,
                    'signature': key,
                    'count': 0,
                    'first': now,
                    'samples': [],
                }
                groups[key] = item
            item['count'] += 1
            item['last'] = now
            item['level'] = level or item['level']
            sample = {'at': now, 'text': message[:500]}
            if context:
                sample['context'] = {k: str(v)[:120] for k, v in context.items()}
            if exc is not None:
                sample['traceback'] = ''.join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )[-2000:]
            item['samples'] = ([sample] + item['samples'])[:MAX_SAMPLES]
            _save()
    except Exception:                              # noqa: BLE001
        pass


def snapshot(limit=100):
    """Группы ошибок, свежие сверху — для дашборда."""
    try:
        with _lock:
            groups = list(_load().values())
        groups.sort(key=lambda g: g.get('last', ''), reverse=True)
        return groups[:limit]
    except Exception:                              # noqa: BLE001
        return []


def summary():
    """Короткая сводка: сколько групп и сколько случаев всего."""
    groups = snapshot(MAX_GROUPS)
    return {
        'groups': len(groups),
        'total': sum(g.get('count', 0) for g in groups),
        'last': groups[0].get('last') if groups else None,
        'categories': sorted({g.get('category', 'прочее') for g in groups}),
    }


def clear():
    """Очистка журнала оператором — после того как разобрался."""
    global _groups
    try:
        with _lock:
            _groups = {}
            _save()
        return True
    except Exception:                              # noqa: BLE001
        return False


# ── Автоматический сбор из общего лога ───────────────────────────────────────

MARKERS = ('⚠️', '❌', '🚨')


def _hook(message, level):
    """
    Забирает из общего лога всё, что похоже на ошибку.

    Расставлять record() по коду вручную бессмысленно: таких мест уже 69, и
    при любой правке появятся новые, о которых никто не вспомнит.
    """
    text = str(message)
    if level in ('ERROR', 'WARN', 'WARNING') or any(m in text[:6] for m in MARKERS):
        record(text.lstrip('⚠️❌🚨 ').strip(),
               level='WARN' if '⚠️' in text[:6] else 'ERROR')


def install():
    """Подключает сбор к logger. Вызывается один раз при старте бота."""
    try:
        import logger
        logger.set_error_hook(_hook)
        return True
    except Exception:                              # noqa: BLE001
        return False
