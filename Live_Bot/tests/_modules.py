"""
Выгрузка и возврат модулей в проверках — с учётом пакетов (этап 10).

После переезда модулей в пакеты (execution/, accounts/, …) боевой код
импортирует их как «from execution import setup_journal». Такой импорт сперва
смотрит АТРИБУТ пакета и только при его отсутствии грузит модуль. Одного
sys.modules.pop('execution.setup_journal') поэтому мало: атрибут пакета
остаётся, и брокер, загруженный заново, получает прежний модуль — с прежним
каталогом данных и прежними подменами. Здесь выгрузка снимает и атрибут, а
возврат ставит его обратно.
"""

import sys


def forget(name, default=None):
    """Убирает модуль из памяти (и из атрибутов пакета). Возвращает прежний."""
    module = sys.modules.pop(name, default)
    parent, _, child = name.rpartition('.')
    package = sys.modules.get(parent) if parent else None
    if package is not None and child in vars(package):
        delattr(package, child)
    return module


def remember(name, module):
    """Возвращает модуль в память (и в атрибут пакета)."""
    sys.modules[name] = module
    parent, _, child = name.rpartition('.')
    package = sys.modules.get(parent) if parent else None
    if package is not None:
        setattr(package, child, module)
