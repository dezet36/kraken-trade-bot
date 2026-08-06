"""
Один запущенный экземпляр приложения, не больше.

ЗАЧЕМ. Проверено запуском: два щелчка по файлу дают два окна и ДВА бота,
которые пишут в один и тот же paper_state.json, positions_state.json и журнал
сделок. Два процесса, ведущие одно состояние позиций, рано или поздно затрут
работу друг друга, а журнал сделок — это то, на чём стоят все замеры проекта.

Сделать это случайно легко: свернул окно, не нашёл его на панели задач,
щёлкнул по файлу ещё раз.

КАК. Именованный мьютекс Windows. Он живёт ровно столько, сколько живёт
процесс: аварийное завершение, отключение питания, снятие через диспетчер
задач — система освобождает его сама. Файл-замок так не умеет, после падения
он остаётся лежать и блокирует запуск навсегда.

Имя мьютекса включает КАТАЛОГ ДАННЫХ, а не просто название программы. Замок
защищает не «приложение вообще», а конкретный набор файлов: две копии,
работающие с разными папками, друг другу не мешают и запускаться должны обе.
"""

import ctypes
import hashlib
import os
import sys

ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9


def _mutex_name(data_dir):
    # Имя мьютекса не может содержать обратный слэш, а путь состоит из них.
    # Хеш заодно избавляет от ограничения на длину имени.
    digest = hashlib.sha1(os.path.abspath(data_dir).lower().encode('utf-8')).hexdigest()
    return f'Local\\KrakenBot-{digest[:16]}'


def acquire(data_dir):
    """
    Занимает замок. False — приложение уже работает с этим каталогом данных.

    Дескриптор намеренно НЕ закрывается и складывается в модуль: пока он
    открыт, мьютекс существует. Освободится он при завершении процесса, что и
    требуется.
    """
    if sys.platform != 'win32':
        return True
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, _mutex_name(data_dir))
        if not handle:
            return True                      # не смогли — не мешаем работать
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        acquire._handle = handle             # держим до конца жизни процесса
        return True
    except Exception:                        # noqa: BLE001
        return True


def focus_existing(title):
    """
    Поднимает окно уже работающего экземпляра.

    Без этого второй запуск выглядел бы как «ничего не произошло»: человек
    щёлкает по файлу, программа молча закрывается, а окно первого экземпляра
    он не нашёл — потому и щёлкнул. Показать его и есть правильный ответ.
    """
    if sys.platform != 'win32':
        return False
    try:
        user32 = ctypes.windll.user32
        window = user32.FindWindowW(None, title)
        if not window:
            return False
        user32.ShowWindow(window, SW_RESTORE)
        user32.SetForegroundWindow(window)
        return True
    except Exception:                        # noqa: BLE001
        return False
