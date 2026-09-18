"""
Окно панели, подключённое к серверу через туннель SSH.

ЗАЧЕМ. Бот живёт на сервере без графической оболочки, а смотреть на него надо
с рабочей машины. Панель и окно у проекта уже есть — не хватало только
соединения.

ПОЧЕМУ ТУННЕЛЬ, А НЕ ОТКРЫТЫЙ ПОРТ. Напрашивалось поставить на сервере
DASHBOARD_HOST=0.0.0.0 и ходить по адресу напрямую. Так делать нельзя, и
причин две.

Первая: у панели нет ни пароля, ни HTTPS. Любой, кто найдёт адрес и порт,
увидит баланс, открытые позиции и всю историю сделок. Порт на публичном
сервере находят перебором за часы.

Вторая неочевидна и решает дело. Дашборд сам защищается от такого доступа:
_controls_allowed() выключает ВСЁ управление, когда сервер слушает не петлевой
адрес. То есть открытый наружу порт не только показывает лишнее посторонним,
но и отбирает у владельца возможность сменить настройку, закрыть позицию или
поставить паузу.

С туннелем оба возражения исчезают. Сервер продолжает слушать 127.0.0.1,
наружу не выставлено ничего, трафик шифрует SSH, а запрос приходит в панель с
петлевого адреса — и управление работает целиком. Туннель здесь не компромисс,
а строго лучший вариант.

ПОЧЕМУ ВСТРОЕННЫЙ КЛИЕНТ SSH, А НЕ БИБЛИОТЕКА. OpenSSH входит в Windows 10 и
новее. Тянуть paramiko значило бы добавить в сборку криптографию, которую
придётся обновлять вслед за уязвимостями, ради работы, которую система уже
умеет. Ключи при этом остаются там, где человек их держит, и программа их не
читает и не копирует.
"""

import json
import os
import socket
import subprocess
import sys
import time

APP_TITLE = 'Kraken — сервер'

# Куда пробрасываем на своей машине. Не 8787: на этой же машине может быть
# запущен и свой бот, и тогда окно показало бы локальные данные вместо
# серверных, ничем не выдав подмены.
LOCAL_PORT = int(os.getenv('REMOTE_LOCAL_PORT', 8799))

# Сколько ждём, пока туннель начнёт принимать соединения.
CONNECT_TIMEOUT = float(os.getenv('REMOTE_CONNECT_TIMEOUT', 20))

SETTINGS_NAME = 'remote.json'


def settings_path():
    """
    Файл настроек рядом с программой, а не в папке данных бота.

    На этой машине бота может не быть вовсе: человек поставил окно, чтобы
    смотреть на сервер, и никакого BOT_DATA_DIR у него нет.
    """
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, SETTINGS_NAME)


def load_settings():
    """Настройки соединения. Пустой словарь, если файла нет."""
    try:
        with open(settings_path(), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    """
    Сохраняет настройки соединения.

    ПАРОЛЕЙ ЗДЕСЬ НЕ ХРАНИТСЯ. Подключение идёт по ключу SSH — тому же, каким
    человек уже ходит на сервер. Пароль в файле рядом с программой рано или
    поздно уедет вместе с папкой на чужую машину.
    """
    with open(settings_path(), 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def ssh_candidates():
    """
    Где искать клиент SSH, по порядку предпочтения.

    ОДНОГО PATH НЕ ХВАТАЕТ, И ЭТО НЕ РЕДКИЙ СЛУЧАЙ. Клиент из комплекта Git
    лежит в Program Files\\Git\\usr\\bin и в системный PATH не добавляется: он
    виден только внутри Git Bash. Проверка через which находила его при запуске
    из Git Bash и не находила при запуске программы обычным способом — то есть
    ровно там, где человек её и запускает.

    Так и вышло: на Windows 10 LTSC 2019 встроенного OpenSSH нет вовсе,
    Git-овский есть, а программа сообщала «клиент не найден» и предлагала
    поставить компонент, который человеку не нужен.
    """
    out = []
    if sys.platform == 'win32':
        system_root = os.environ.get('SystemRoot', r'C:\Windows')
        out.append(os.path.join(system_root, 'System32', 'OpenSSH', 'ssh.exe'))
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                     os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs')):
            if base:
                out.append(os.path.join(base, 'Git', 'usr', 'bin', 'ssh.exe'))
    return out


def ssh_exe():
    """Путь к клиенту SSH. None, если его нет нигде."""
    for path in ssh_candidates():
        if os.path.exists(path):
            return path
    from shutil import which
    return which('ssh')


def tunnel_command(cfg, ssh):
    """
    Команда запуска туннеля.

    -N   не выполнять команд на сервере: нам нужен только проброс
    -T   не запрашивать терминал
    ExitOnForwardFailure — падать сразу, если порт занять не удалось. Без
         этого ssh остаётся жить, окно открывается и показывает пустоту, а
         причина теряется.
    ServerAliveInterval — рвать зависшее соединение, а не держать мёртвый
         туннель, за которым окно показывает устаревшие числа.
    """
    remote_port = int(cfg.get('remote_port') or 8787)
    args = [ssh, '-N', '-T',
            '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3',
            # ОТПЕЧАТОК ПРИНИМАЕТСЯ ПРИ ПЕРВОМ ПОДКЛЮЧЕНИИ, НО НЕ ПРИ СМЕНЕ.
            #
            # По умолчанию ssh при незнакомом сервере спрашивает «yes/no» — а
            # спросить некому: у программы нет терминала, и она просто
            # сообщала «отпечаток не подтверждён», отправляя человека в
            # консоль делать руками то же самое.
            #
            # accept-new запоминает отпечаток при ПЕРВОМ подключении, ровно как
            # ответ «yes». Защита при этом остаётся той, которая и важна: если
            # отпечаток ПОМЕНЯЕТСЯ, соединение откажет громко. Именно смена
            # означает подмену сервера, а не первое знакомство с ним.
            #
            # Слабее StrictHostKeyChecking=no, который молчит и при смене, —
            # вот его ставить было бы нельзя.
            '-o', 'StrictHostKeyChecking=accept-new',
            '-L', f'{LOCAL_PORT}:127.0.0.1:{remote_port}']
    if cfg.get('key'):
        args += ['-i', cfg['key'], '-o', 'IdentitiesOnly=yes']
    if cfg.get('ssh_port'):
        args += ['-p', str(cfg['ssh_port'])]
    args.append(f"{cfg.get('user') or 'root'}@{cfg['host']}")
    return args


def tie_to_parent(process):
    """
    Привязывает туннель к жизни программы: умрёт она — умрёт и он.

    ЗАЧЕМ. process.terminate() в finally закрывает ssh при НОРМАЛЬНОМ выходе. Но
    если программу снять принудительно — диспетчером задач, Stop-Process, при
    пересборке, — finally не выполняется, и ssh остаётся жить сиротой, держа
    порт 8799.

    Следующий запуск видит занятый порт, отказывается подключаться и
    возвращается к окну настроек. Со стороны это выглядит как «программа
    перестала работать»: окно открывается, а данных нет. Ровно так и вышло при
    отладке — час ушёл на поиск причины, которой был осиротевший процесс.

    Задание Windows (Job Object) с флагом KILL_ON_JOB_CLOSE решает это на
    уровне системы: ссылка на задание живёт в нашем процессе, и как только он
    исчезает ЛЮБЫМ способом, система убивает всё, что в задание входит.

    Молча ничего не делает там, где не применимо: это удобство, а не условие
    работы.
    """
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                        ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.POINTER(ctypes.c_ulong)),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('ReadOperationCount', 'WriteOperationCount',
                         'OtherOperationCount', 'ReadTransferCount',
                         'WriteTransferCount', 'OtherTransferCount')]

        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC),
                        ('IoInfo', COUNTERS),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            return None

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info),
                                              ctypes.sizeof(info)):
            return None

        handle = kernel.OpenProcess(0x1F0FFF, False, process.pid)
        if not handle:
            return None
        kernel.AssignProcessToJobObject(job, handle)
        kernel.CloseHandle(handle)
        # Ссылку возвращаем, чтобы вызывающий её сохранил: закроется она —
        # закроется и задание, и ssh умрёт.
        return job
    except Exception:                                  # noqa: BLE001
        return None


def port_open(port, host='127.0.0.1'):
    """Принимает ли кто-то соединения на этом порту."""
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def wait_for_tunnel(process, timeout=CONNECT_TIMEOUT):
    """
    Ждёт, пока туннель заработает. Возвращает (готов, сообщение об ошибке).

    Следим и за процессом тоже: ssh падает быстрее, чем истекает ожидание, и
    ждать после его смерти полные двадцать секунд — значит заставлять человека
    смотреть на пустое окно вместо причины отказа.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            out = ''
            try:
                out = (process.stderr.read() or b'').decode('utf-8', 'replace')
            except Exception:                      # noqa: BLE001
                pass
            return False, out.strip() or 'клиент SSH завершился без объяснения'
        if port_open(LOCAL_PORT):
            return True, ''
        time.sleep(0.3)
    return False, f'туннель не поднялся за {timeout:.0f} с'


def explain(error):
    """
    Переводит отказ SSH на человеческий и говорит, что делать.

    Сообщения клиента SSH точны и бесполезны для того, кто видит их впервые.
    Разбор по подстроке груб, но ошибиться тут нечем: непонятое сообщение
    показывается как есть.
    """
    low = (error or '').lower()
    if 'permission denied' in low:
        return ('Сервер не принял ключ. Проверьте, что он добавлен в '
                '~/.ssh/authorized_keys на сервере и что в настройках указан '
                'верный файл ключа.')
    if 'could not resolve' in low or 'name or service' in low:
        return 'Адрес сервера не найден. Проверьте имя или IP.'
    if 'connection refused' in low:
        return ('Сервер отказал в соединении. Проверьте, что он включён и что '
                'SSH слушает указанный порт.')
    if 'connection timed out' in low or 'timed out' in low:
        return ('Сервер не отвечает. Проверьте адрес и что порт SSH не закрыт '
                'брандмауэром.')
    if 'host key verification failed' in low:
        return ('Отпечаток сервера изменился или не подтверждён. Подключитесь '
                'к серверу один раз обычным ssh и подтвердите отпечаток.')
    if 'address already in use' in low or 'cannot listen' in low:
        return (f'Порт {LOCAL_PORT} на этой машине занят. Закройте второе окно '
                f'программы или задайте REMOTE_LOCAL_PORT.')
    return error or 'причина неизвестна'


def open_tunnel(cfg):
    """
    Поднимает туннель. Возвращает (процесс, ошибка).

    Процесс не демон: его надо закрыть явно при выходе, иначе он переживёт
    окно и займёт порт до перезагрузки.
    """
    ssh = ssh_exe()
    if not ssh:
        # Перечисляем, ГДЕ искали. Без этого «не найден» отправляет ставить
        # компонент Windows человеку, у которого клиент уже есть в составе Git,
        # просто в другом месте.
        looked = '\n'.join(f'  {path}' for path in ssh_candidates())
        return None, ('Клиент SSH не найден. Искал здесь:\n' + looked +
                      '\nи в PATH.\n\n'
                      'Поставьте любой из двух: Параметры → Приложения → '
                      'Дополнительные компоненты → Клиент OpenSSH, либо Git '
                      'for Windows (в его составе клиент уже есть).')
    if not cfg.get('host'):
        return None, 'Не задан адрес сервера.'
    if port_open(LOCAL_PORT):
        return None, (f'Порт {LOCAL_PORT} уже занят — вероятно, программа '
                      f'запущена вторым окном.')

    flags = 0
    if sys.platform == 'win32':
        # Без этого поверх окна на секунду выскакивает чёрная консоль ssh.
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    process = subprocess.Popen(tunnel_command(cfg, ssh),
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE,
                               creationflags=flags)
    ok, error = wait_for_tunnel(process)
    if not ok:
        try:
            process.terminate()
        except Exception:                          # noqa: BLE001
            pass
        return None, explain(error)

    # Ссылка на задание хранится ПРЯМО НА ПРОЦЕССЕ, а не в локальной
    # переменной: местная переменная исчезнет при выходе из функции, задание
    # закроется, и ssh будет убит сразу после успешного подключения.
    process._job = tie_to_parent(process)
    return process, ''


def url():
    return f'http://127.0.0.1:{LOCAL_PORT}/'
