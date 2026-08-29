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

# ── Соседи ──────────────────────────────────────────────────────────────────
#
# Замок выше защищает КАТАЛОГ ДАННЫХ, и это верно: две копии с разными
# папками не портят файлы друг другу. Но торгуют они один рынок.
#
# Разбор 364 сделок с сервера за 5–29 августа 2026 показал, чем это кончается.
# Две копии — из исходников и собранная — работали 23 дня одновременно, каждая
# со своим счётчиком сделок и своей цепочкой баланса. 120 сигналов были взяты
# ДВАЖДЫ: та же пара, тот же стоп, та же цель. Риск на идею оказался вдвое
# выше заявленного, а измерения — смесью двух опытов с разными настройками
# (у одной копии зона B считалась от 61.8%, у другой от 78.6%).
#
# Заметить это было нечем: копии друг о друге не знают по устройству. Поэтому
# каждая отмечается в общем на машину месте, а диагностика показывает соседей.
# ЗАПРЕЩАТЬ здесь нельзя — две копии с разными папками бывают нужны (проверка
# новой сборки рядом с рабочей). Решение за человеком, дело кода — показать.

def _registry_dir():
    base = (os.environ.get('LOCALAPPDATA') or os.environ.get('TMPDIR')
            or os.environ.get('TMP') or '/tmp')
    return os.path.join(base, 'KrakenBot', 'instances')


def register(data_dir):
    """Отмечает эту копию в общем на машину списке. Тихо, как и mark_running."""
    try:
        import json
        import time
        d = _registry_dir()
        os.makedirs(d, exist_ok=True)
        digest = hashlib.sha1(os.path.abspath(data_dir).lower()
                              .encode('utf-8')).hexdigest()[:16]
        with open(os.path.join(d, f'{digest}.json'), 'w', encoding='utf-8') as fh:
            json.dump({'pid': os.getpid(), 'data_dir': os.path.abspath(data_dir),
                       'started': time.time()}, fh, ensure_ascii=False)
    except Exception:                              # noqa: BLE001
        pass                                       # отметка — удобство, не условие


def _alive(pid):
    """
    Жив ли процесс. Мёртвые отметки остаются после аварийного завершения, и
    принимать их за работающую копию значит пугать человека призраком.
    """
    if not pid or pid <= 0:
        return False
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    PROCESS_QUERY_LIMITED = 0x1000
    STILL_ACTIVE = 259
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k.CloseHandle(h)
    except Exception:                              # noqa: BLE001
        return False


def siblings(data_dir):
    """
    Другие ЖИВЫЕ копии на этой машине. Список словарей с pid и data_dir.

    Своя копия и мёртвые отметки не возвращаются. Отметку мёртвой копии сразу
    убираем: иначе список растёт с каждым падением и однажды перестаёт что-то
    значить.
    """
    import json
    out = []
    mine = os.path.abspath(data_dir).lower()
    d = _registry_dir()
    try:
        names = os.listdir(d)
    except Exception:                              # noqa: BLE001
        return out
    for name in names:
        if not name.endswith('.json'):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, encoding='utf-8') as fh:
                info = json.load(fh)
        except Exception:                          # noqa: BLE001
            continue
        pid = info.get('pid')
        if pid == os.getpid() or str(info.get('data_dir', '')).lower() == mine:
            continue
        if not _alive(pid):
            try:
                os.remove(path)
            except Exception:                      # noqa: BLE001
                pass
            continue
        out.append(info)
    return out
