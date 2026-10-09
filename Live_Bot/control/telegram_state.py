"""
Состояние Telegram-панели на диске: пауза, «без звука», день последней сводки.

ЗАЧЕМ НА ДИСКЕ. До 26.09.2026 пауза и «без звука» жили в памяти процесса, а
бот перезапускается при каждой выкатке — по нескольку раз в день. Нажатая
«пауза» молча снималась первой же выкаткой, и новые входы шли, хотя человек
их остановил. Дневная сводка по той же причине уходила заново после каждого
перезапуска.

Запись — через временный файл и атомарную замену (правило проекта).
"""

import json
import os
import threading

_lock = threading.Lock()


def path():
    # Каждый раз через sys.modules: проверки перезагружают config с другой
    # папкой данных, и путь, схваченный при импорте, указывал бы в чужую.
    import config
    return os.path.join(config.DATA_DIR, 'telegram_state.json')


def load():
    try:
        with open(path(), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update(**changes):
    """Меняет поля и пишет файл целиком. Отказ записи торговле не мешает."""
    with _lock:
        data = load()
        data.update(changes)
        target = path()
        try:
            tmp = target + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, target)
        except OSError:
            pass
        return data
