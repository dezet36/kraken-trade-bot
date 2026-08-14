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

    А ВОТ ЧУЖОЙ ДЕСКРИПТОР ЗАКРЫВАТЬ ОБЯЗАТЕЛЬНО, и на этом всё ломалось.
    CreateMutexW отдаёт дескриптор ДАЖЕ КОГДА мьютекс уже существует — просто
    сообщает об этом кодом ошибки. Раньше мы в этом случае возвращали False и
    дескриптор бросали открытым. Пока он открыт, объект живёт: прежняя копия
    давно закрыта, а замок всё ещё «занят» — нами же. Второй вызов из того же
    процесса не проходил уже никогда.

    Из-за этого «закрыть прежнюю копию и продолжить» было невозможно в
    принципе: сколько ни жди, замок не освободится. Проверено — пять попыток
    подряд после гибели прежней копии отвечали False.
    """
    if sys.platform != 'win32':
        return True
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, _mutex_name(data_dir))
        if not handle:
            return True                      # не смогли — не мешаем работать
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        acquire._handle = handle             # держим до конца жизни процесса
        return True
    except Exception:                        # noqa: BLE001
        return True


def close_previous(data_dir, wait=25):
    """
    Закрывает прежнюю копию и забирает замок. True — замок наш.

    ЗАЧЕМ ЭТО ОТДЕЛЬНО ОТ ОСВОБОЖДЕНИЯ ПОРТА. Замок и порт держат разные вещи,
    и прежняя копия может держать замок, УЖЕ отпустив порт: окно закрыто,
    сервер остановлен, а процесс ещё доживает свои секунды. Запуск после
    обновления в этот момент упирался в замок — до проверки порта дело даже не
    доходило, — показывал «Программа уже работает» и выходил. Человеку
    оставался диспетчер задач, и он это и делал.

    Закрываем строго по PID, который копия записала о себе САМА в running_app.
    Это не догадка по имени процесса: файл пишем мы, и никто другой в него не
    попадает.
    """
    import subprocess
    import time

    info = running_info(data_dir)
    try:
        pid = int(info.get('pid') or 0)
    except (TypeError, ValueError):
        pid = 0
    if not pid or pid == os.getpid():
        return acquire(data_dir)

    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/F', '/T'],
                       capture_output=True, timeout=20, creationflags=flags)
    except Exception:                        # noqa: BLE001
        pass                                 # не вышло — всё равно подождём

    for _ in range(max(1, wait)):
        if acquire(data_dir):
            return True
        time.sleep(1)
    return acquire(data_dir)


MARK_FILE = 'running_app.json'


def mark_running(data_dir, version, exe_path):
    """
    Оставляет отметку о том, ЧТО именно сейчас работает.

    Нужна второму запуску. Без неё он умеет только поднять чужое окно и
    молча выйти — и человек, скачавший новую версию, видит открывшееся окно
    старой и уверен, что обновился. Ровно так и вышло: новый файл скачали,
    щёлкнули, окно появилось, а версия осталась прежней.
    """
    try:
        import json
        with open(os.path.join(data_dir, MARK_FILE), 'w', encoding='utf-8') as fh:
            json.dump({'version': version or 'без версии', 'exe': exe_path,
                       'pid': os.getpid()}, fh, ensure_ascii=False)
    except Exception:                              # noqa: BLE001
        pass                                       # отметка — удобство, не условие


def running_info(data_dir):
    """Что записал работающий экземпляр. Пустой словарь, если непонятно."""
    try:
        import json
        with open(os.path.join(data_dir, MARK_FILE), encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:                              # noqa: BLE001
        return {}


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
