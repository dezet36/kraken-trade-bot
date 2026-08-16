"""
Настольное приложение: одна иконка запускает бота и открывает окно с дашбордом.

Как это устроено. Бот работает в фоновом потоке, дашборд поднимается на
localhost, а на переднем плане открывается ОТДЕЛЬНОЕ окно (не вкладка
браузера): у него своя кнопка на панели задач, свой значок и никакой адресной
строки. Способ выбирается по тому, что есть на машине:

    1. pywebview        — настоящее нативное окно (если пакет установлен);
    2. Chrome/Edge      — режим --app: окно без браузерного обвеса, свой профиль,
                          поэтому процесс наш и закрытие окна закрывает программу;
    3. обычный браузер  — крайний случай, чтобы не остаться совсем без интерфейса.

Консоли в оконном режиме нет, поэтому две вещи сделаны намеренно:
    * журнал работы виден прямо в дашборде (панель «Журнал работы»);
    * состояние бота отдаётся в дашборд через dashboard.set_status, иначе
      упавший фоновый поток остался бы незамеченным — окно продолжало бы
      показывать последние данные как ни в чём не бывало.

Закрытие окна ОСТАНАВЛИВАЕТ бота. Состояние на диске сохраняется, следующий
запуск продолжает с того же места. Для месячного прогона без присмотра лучше
консольный запуск (start.bat) или служба Windows — окно можно случайно закрыть.

Запуск: pythonw Live_Bot/desktop.py   (или ярлык, созданный install_desktop.ps1)
"""

import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config                      # noqa: E402
import dashboard                   # noqa: E402
from logger import log             # noqa: E402

APP_TITLE = 'Kraken — торговый бот'
# Сколько ждём ответа дашборда. Ждём ЛЁГКУЮ страницу (см. _wait_for_dashboard),
# поэтому порога хватает с запасом: замер на запуске дал 13 секунд.
STARTUP_TIMEOUT = 90               # сколько ждём ответа дашборда, секунд
SHUTDOWN_TIMEOUT = 45              # сколько ждём снятия заявок при закрытии

CHROME_PATHS = (
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
)


# ── Диалоги (консоли нет, сообщать об ошибке больше нечем) ───────────────────

def _ask(title, message):
    """Вопрос «да/нет» окном. Спросить не у кого — считаем ответ «нет»."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        answer = messagebox.askyesno(title, message)
        root.destroy()
        return bool(answer)
    except Exception:                              # noqa: BLE001
        log(f'{title}: {message} — спросить не у кого, отвечаю «нет»')
        return False


def _alert(title, message):
    """Показывает сообщение окном. Если tkinter недоступен — пишет в лог."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(title, message)
        root.destroy()
    except Exception:
        log(f"{title}: {message}")


# ── Бот в фоне ───────────────────────────────────────────────────────────────

def _run_bot():
    """Крутит обычный цикл бота, но сообщает о падении в окно."""
    try:
        import bot
        dashboard.set_status('running')
        bot.main()
        dashboard.set_status('stopped', 'цикл завершён')
    except SystemExit as exc:
        # bot.main() зовёт sys.exit(1), когда не смог подключиться к бирже.
        # В потоке это тихо убило бы бота, а окно продолжало бы висеть пустым.
        dashboard.set_status('error', 'не удалось подключиться к бирже')
        log(f"❌ Бот завершился с кодом {exc.code}")
    except Exception as exc:
        import traceback
        log(f"❌ Бот остановлен ошибкой: {exc}")
        log(traceback.format_exc())
        dashboard.set_status('error', str(exc))


def _wait_for_dashboard(url, timeout=STARTUP_TIMEOUT):
    """
    Ждёт, пока дашборд начнёт отвечать: окно нельзя открывать раньше.

    ЖДЁМ САМУЮ ЛЁГКУЮ СТРАНИЦУ, А НЕ САМУЮ ТЯЖЁЛУЮ. Здесь ждали `/api/data` —
    ту, что считает индикаторы по двум десяткам пар. Замерено на запуске:

        /api/whoami   ответил через 13 секунд
        /api/data     ответил через 49 секунд
        порог                       40 секунд

    Девяти секунд не хватило, и дальше всё шло по худшему пути: показывалось
    окно с ошибкой — в оконной сборке НЕВИДИМОЕ, — и приложение вставало на нём
    навсегда. Снаружи: бот торгует, дашборд отвечает, окна нет и в журнале ни
    строчки. Вопрос «почему нет окна» разбирался час.

    Вопрос, на который мы отвечаем, — «поднялся ли сервер», и лёгкая страница
    отвечает на него ровно так же, только вовремя.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.4)
    return False


# ── Окно ─────────────────────────────────────────────────────────────────────

GEOMETRY_FILE = os.path.join(config.DATA_DIR, 'window.json')


def _load_geometry():
    """Размер и положение окна с прошлого раза."""
    try:
        import json
        with open(GEOMETRY_FILE, encoding='utf-8') as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return {}
    # Окно, сохранённое на втором мониторе, которого больше нет, оказалось бы
    # за пределами экрана — и человек решил бы, что программа не запускается.
    if not isinstance(saved, dict) or saved.get('width', 0) < 600:
        return {}
    return saved


def _save_geometry(window):
    try:
        import json
        data = {'width': int(window.width), 'height': int(window.height),
                'x': int(window.x), 'y': int(window.y)}
        with open(GEOMETRY_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
    except Exception:                          # noqa: BLE001
        pass


def _open_positions_count():
    """Сколько позиций бот ведёт прямо сейчас."""
    try:
        import dashboard as dash
        book = dash._broker or dash._trade_manager
        if book is None:
            return 0
        snapshot = book.snapshot() if hasattr(book, 'snapshot') else None
        if snapshot is not None:
            return len([p for p in snapshot.get('open', []) if not p.get('pending')])
        return len(getattr(book, 'positions', {}) or {})
    except Exception:                          # noqa: BLE001
        return 0


def _confirm_close():
    """
    Спрашивает перед закрытием, когда бот ведёт позиции.

    Закрытие окна останавливает бота полностью — это проверено, зомби не
    остаётся. Но именно поэтому у закрытия есть цена: стопы стоят на бирже и
    сработают сами, а перевод в безубыток, частичные фиксации и выход по
    времени делает бот. Закрыв окно с открытыми позициями, человек оставляет
    их без ведения, и никто ему об этом не говорил.

    Возврат False отменяет закрытие.

    Здесь же запоминается размер и положение окна. В событии «закрыто» это
    делать поздно: окна к тому моменту уже нет, и читать его геометрию не у
    чего — проверено, файл не создавался.
    """
    try:
        import webview
        _save_geometry(webview.windows[0])
    except Exception:                          # noqa: BLE001
        pass

    count = _open_positions_count()
    if not count:
        return True
    try:
        import webview
        window = webview.windows[0]
        return window.create_confirmation_dialog(
            APP_TITLE,
            f'Бот ведёт открытых позиций: {count}.\n\n'
            'Закрытие окна останавливает бота. Стопы останутся на бирже и '
            'сработают сами, но перевод в безубыток, частичные фиксации и '
            'выход по времени делать будет некому.\n\n'
            'Всё равно закрыть?')
    except Exception:                          # noqa: BLE001
        return True                            # спросить не вышло — не мешаем


def _open_native(url):
    """
    Своё окно через pywebview. None — открыть не удалось, зовите запасной путь.

    Движок задаётся явно: edgechromium — это WebView2, встроенный в Windows 10
    и новее. Без явного указания pywebview может выбрать mshtml, движок
    Internet Explorer, и дашборд в нём разъедется — там нет ни grid, ни
    современного CSS, на котором он собран.
    """
    try:
        import webview
    except ImportError:
        return None

    saved = _load_geometry()
    window = webview.create_window(
        APP_TITLE, url,
        width=saved.get('width', 1360), height=saved.get('height', 900),
        x=saved.get('x'), y=saved.get('y'),
        min_size=(1000, 640))

    window.events.closing += _confirm_close

    gui = 'edgechromium' if sys.platform == 'win32' else None
    try:
        webview.start(gui=gui)          # блокирует до закрытия окна
        return True
    except Exception as exc:            # noqa: BLE001
        # Чаще всего это отсутствующий WebView2 на старой Windows. Падать
        # нельзя: ниже есть запасные пути, а интерфейс нужен человеку сейчас.
        log(f"Своё окно не открылось ({exc}) — пробую запасной способ")
        return None


def _open_app_window(url):
    """
    Окно браузера в режиме приложения: без адресной строки и вкладок.

    Отдельный профиль (--user-data-dir) нужен по двум причинам: окно получает
    собственную кнопку на панели задач, а не подклеивается к уже открытому
    браузеру, и запущенный процесс живёт до закрытия окна — иначе программа
    завершилась бы сразу после старта.
    """
    browser = next((path for path in CHROME_PATHS if os.path.exists(path)), None)
    if not browser:
        return None

    profile = os.path.join(config.DATA_DIR, 'app_window')
    os.makedirs(profile, exist_ok=True)
    return subprocess.Popen([
        browser,
        f'--app={url}',
        f'--user-data-dir={profile}',
        '--window-size=1280,860',
        '--no-first-run',
        '--no-default-browser-check',
    ])


def _shutdown():
    """
    Доводит закрытие до конца: снимает заявки и завершает процесс.

    ПОЧЕМУ ВЫХОД ПРИНУДИТЕЛЬНЫЙ. Возврата из main() для завершения НЕ хватает.
    Интерпретатор перед выходом дожидается всех недемонских потоков, а их у
    нас заводит не только наш код: пул планировщика создаёт рабочие потоки, и
    concurrent.futures вешает обработчик, который их join-ит. Приложение
    остаётся в памяти после закрытия окна — вместе с портом дашборда и замком
    на второй экземпляр. Снаружи это выглядит так: окно закрыл, значка нет, а
    следующий запуск говорит «порт занят». Ровно это пользователь и разбирал
    руками через диспетчер задач, раз за разом.

    Про потерю состояния можно не беспокоиться: и фантомный счёт, и позиции
    пишутся на диск в конце каждого цикла, а не при выходе.

    Стопы на бирже стоят сами и переживут закрытие: это их работа. Ведение
    позиции — перевод в безубыток, частичные фиксации, выход по времени — с
    закрытием прекращается, и об этом человека спрашивают отдельно (_confirm_close).
    """
    log('Окно приложения закрыто — бот остановлен')
    logging.shutdown()
    os._exit(0)


def _open_window(url):
    """
    Открывает интерфейс лучшим доступным способом и ждёт его закрытия.

    ОКНО БРАУЗЕРА ПРОБУЕТСЯ ПЕРВЫМ, И ЭТО ИСПРАВЛЕНИЕ ПО ЖИВОЙ ПОЛОМКЕ.
    Прежде первым шёл pywebview — «настоящее нативное окно». На этой машине он
    молча не работает: окно WinForms создаётся, а движок внутри падает —

        CoreWebView2Environment.CreateCoreWebView2ControllerAsync → исключение

    Беда в том, КАК он падает. Исключение съедается внутри асинхронной
    инициализации, webview.start() не возвращает ошибки и просто блокируется
    навсегда. Снаружи это выглядит так: приложение запущено, дашборд отвечает
    по сети, окна нет и в журнале ни строчки. Человек трижды запускал .exe и
    трижды не понимал, что происходит.

    Запасной путь при этом был готов и не срабатывал — до него не доходило.

    Окно Chrome в режиме приложения даёт ровно то же: своя кнопка на панели
    задач, без адресной строки и вкладок. Native-окно не давало ничего сверх
    этого, а ломалось молча. pywebview остаётся ниже — на машинах без Chrome.
    """
    # КАЖДЫЙ ПУТЬ НАЗЫВАЕТ СЕБЯ В ЖУРНАЛЕ, И ЭТО НЕ МНОГОСЛОВНОСТЬ.
    #
    # Разбор «почему нет окна» занял час именно потому, что журнал молчал.
    # Приложение работало, дашборд отвечал, а какой из трёх путей выбран и чем
    # он кончился — узнать было неоткуда. Одна строка на путь снимает вопрос.
    log('Окно: пробую окно браузера в режиме приложения')
    process = _open_app_window(url)
    if process is not None:
        log('Окно: открыто окном браузера')
        process.wait()
        log('Окно: закрыто пользователем')
        return
    log('Окно: браузер не найден — пробую своё окно')

    if _open_native(url):
        log('Окно: своё окно закрыто пользователем')
        return

    import webbrowser
    webbrowser.open(url)
    log('Окно приложения недоступно — интерфейс открыт в браузере. '
        'Программа продолжит работать до закрытия этого окна.')
    threading.Event().wait()


# ── Точка входа ──────────────────────────────────────────────────────────────

def selftest():
    """
    Проверка собранного приложения: всё ли уехало внутрь .exe.

    Единственная поломка, которой славится упаковка, — потерянный модуль.
    PyInstaller ищет импорты статически и не видит тех, что подгружаются по
    имени в рантайме: ccxt.bybit, планировщик, движок окна. Собранное
    приложение при этом собирается без единой жалобы и падает при первом
    обращении к бирже.

    Проверять это «приложение прожило минуту» нельзя: без ключей оно живёт
    ровно так же — держит открытым окно настройки. Поэтому здесь импорт
    всего, что нужно в работе, и ненулевой код при первой же потере.
    """
    modules = ('bot', 'exchange', 'dashboard', 'trade_manager', 'paper_broker',
               'strategy', 'strategy_smc', 'strategy_levels', 'first_run',
               'updater', 'updater_app', 'ccxt.bybit', 'ccxt.bingx',
               'apscheduler.schedulers.blocking', 'tkinter', 'tkinter.ttk',
               'webview', 'clr', 'pandas', 'numpy')
    import importlib
    failed = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:                   # noqa: BLE001
            failed.append(f'{name}: {exc}')
    # Пишем в файл, а не в stdout: приложение собрано с --windowed, консоли у
    # него нет, и напечатанное просто пропадёт.
    report = os.path.join(config.DATA_DIR, 'selftest.log')
    lines = ([f'НЕ ЗАГРУЗИЛОСЬ {name}' for name in failed]
             or [f'самопроверка пройдена: {len(modules)} модулей на месте'])

    # УЗНАЁТ ЛИ СБОРКА САМА СЕБЯ. Потерянный модуль — не единственная беда
    # упаковки. У пользователя собранное приложение сочло себя запущенным из
    # исходников и предложило обновляться через git, которого рядом нет.
    # Собиралось оно при этом без единой жалобы, и самопроверка молчала.
    # Теперь молчать не будет.
    try:
        import updater
        import updater_app

        version = updater_app.current_version()
        frozen = bool(getattr(sys, 'frozen', False))
        mode = 'выпуск' if updater._app_mode() is not None else 'ИСХОДНИКИ'
        lines.append(f'версия: {version or "ФАЙЛА VERSION НЕТ"}')
        lines.append(f'sys.frozen: {frozen}')
        lines.append(f'обновление считает это: {mode}')
        if mode != 'выпуск':
            lines.insert(0, 'СБОРКА НЕ УЗНАЁТ СЕБЯ: обновление пойдёт через git')
            failed.append('распознавание выпуска')
    except Exception as exc:                       # noqa: BLE001
        lines.append(f'проверка распознавания не отработала: {exc}')
        failed.append('распознавание выпуска')
    try:
        with open(report, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
    except OSError:
        pass
    return 1 if failed else 0


def diag():
    """
    Куда приложение смотрит и что там нашло.

    Нужно затем, что почти все недоумения при работе с окном сводятся к
    одному вопросу: КАКОЙ каталог данных приложение считает своим. Он не
    всегда лежит рядом с .exe — машинная переменная BOT_DATA_DIR, если её
    кто-то однажды поставил (например, установщик службы), перекрывает всё, и
    приложение читает чужой .env, а свой не находит. Догадаться об этом по
    поведению нельзя, а по этому отчёту — сразу.
    """
    import first_run
    key = (os.getenv(f'{config.EXCHANGE_NAME.upper()}_API_KEY') or '').strip()
    lines = [
        f'файл приложения:      {sys.executable}',
        f'каталог данных:       {config.DATA_DIR}',
        f'BOT_DATA_DIR:         {os.getenv("BOT_DATA_DIR") or "(не задана)"}',
        f'.env:                 {first_run.ENV_PATH}',
        f'.env существует:      {os.path.exists(first_run.ENV_PATH)}',
        f'спросит ли ключи:     {first_run.needs_setup()}',
        f'ключ найден:          {"да, " + key[:6] + "…" if key else "нет"}',
        f'биржа:                {config.EXCHANGE_NAME}',
        f'режим:                {config.TRADING_MODE}',
        f'стратегии:            {config.STRATEGY}',
        f'пар в пуле:           {len(config.TRADING_PAIRS_POOL)}',
        f'порт дашборда:        {config.DASHBOARD_PORT}',
    ]
    report = os.path.join(config.DATA_DIR, 'diag.log')
    try:
        with open(report, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
    except OSError as exc:
        lines.append(f'ОТЧЁТ НЕ ЗАПИСАЛСЯ: {exc}')
    _alert(APP_TITLE, '\n'.join(lines))
    return 0


def port_busy(port, host='127.0.0.1'):
    """
    Занят ли порт дашборда кем-то ещё.

    ПОЧЕМУ ЭТОГО НЕ ХВАТАЛО РАНЬШЕ. Готовность проверялась запросом к
    http://127.0.0.1:порт/api/data — и запрос успешно получал ответ ОТ ЧУЖОГО
    сервера, если тот уже слушал этот порт. Приложение считало, что поднялось,
    и открывало окно на дашборде другого процесса. Со стороны это выглядит
    так: запустил новую версию, а в окне старая. Именно так и вышло у
    пользователя — семь часов работал бот, запущенный из исходников
    (pythonw desktop.py), и новый .exe показывал его интерфейс.

    Проверка «отвечает ли кто-то» отвечает не на тот вопрос. Правильный —
    «свободен ли порт», и он решается попыткой занять его.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Без SO_REUSEADDR: нам нужно узнать, что порт ЗАНЯТ, а не одолжить
        # его у чужого сокета в состоянии TIME_WAIT.
        probe.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def port_holder(port):
    """
    Кто держит порт: {'pid', 'name', 'cmd'} либо пустой словарь.

    Командная строка нужна не для красоты сообщения. По ней отличается СВОЯ
    прежняя копия — бот, запущенный из исходников как `pythonw desktop.py`, —
    от постороннего процесса, случайно занявшего тот же порт. Своего можно
    предложить закрыть, чужого трогать нельзя.
    """
    if sys.platform != 'win32':
        return {}
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'tcp'],
                             capture_output=True, text=True, timeout=15,
                             creationflags=flags).stdout
        pid = ''
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == 'TCP'                     and parts[1].endswith(f':{port}') and parts[3] == 'LISTENING':
                pid = parts[4]
                break
        if not pid:
            return {}
        # WMIC УДАЛЁН ИЗ СВЕЖИХ WINDOWS, и без запасного пути командная строка
        # оставалась пустой — а по ней опознавалась своя же копия. Пробуем его
        # первым (он быстрее), но не полагаемся.
        query = ''
        try:
            query = subprocess.run(
                ['wmic', 'process', 'where', f'ProcessId={pid}',
                 'get', 'Name,CommandLine', '/format:list'],
                capture_output=True, text=True, timeout=15,
                creationflags=flags).stdout
        except Exception:                          # noqa: BLE001
            query = ''
        if not query.strip():
            ps = ('Get-CimInstance Win32_Process -Filter "ProcessId=' + str(pid)
                  + '" | ForEach-Object { "Name=" + $_.Name; '
                    '"CommandLine=" + $_.CommandLine }')
            try:
                query = subprocess.run(
                    ['powershell', '-NoProfile', '-Command', ps],
                    capture_output=True, text=True, timeout=20,
                    creationflags=flags).stdout
            except Exception:                      # noqa: BLE001
                query = ''
        name, cmd = '', ''
        for line in query.splitlines():
            if line.startswith('Name='):
                name = line.split('=', 1)[1].strip()
            elif line.startswith('CommandLine='):
                cmd = line.split('=', 1)[1].strip()
        if not name:
            names = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV'],
                capture_output=True, text=True, timeout=10,
                creationflags=flags).stdout
            name = names.split(',')[0].strip('" \r\n') if ',' in names else ''
        return {'pid': pid, 'name': name, 'cmd': cmd}
    except Exception:                              # noqa: BLE001
        return {}


def port_owner(port):
    """Владелец порта одной строкой — для сообщений."""
    holder = port_holder(port)
    if not holder:
        return ''
    return (f"{holder['name']} (PID {holder['pid']})" if holder.get('name')
            else f"PID {holder['pid']}")


def asks_the_port(port, host='127.0.0.1'):
    """
    Спрашивает у самого порта, кто его держит. Самый надёжный способ опознания.

    ПОЧЕМУ НЕ ПО ИМЕНИ ПРОЦЕССА. Имя и командная строка врут в слишком многих
    случаях: запуск из исходников выглядит как `python.exe -c ...`, собранное
    приложение поднимает дочерний процесс, служба — третий вид, а свежие
    Windows вовсе выбросили wmic, которым командная строка и добывалась.
    Достаточно любого из этих случаев, чтобы своя же прежняя копия была
    объявлена «посторонней программой» — после чего обновлённая версия
    отказывалась её закрывать и вставала намертво с сообщением о занятом порте.
    Человеку оставался диспетчер задач.

    Ответ на /api/whoami даёт только наше приложение. По нему видно и то, что
    копия наша, и какой она версии.
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(
                f'http://{host}:{port}/api/whoami', timeout=4) as answer:
            data = json.loads(answer.read().decode('utf-8'))
        return data if data.get('app') == 'kraken-trade-bot' else None
    except Exception:                              # noqa: BLE001
        # Молчит или отвечает чужим — не наш, и это не ошибка, а ответ.
        return None


def our_recorded_pid():
    """
    PID, который прежняя копия записала о себе сама. 0 — записи нет.

    Это самый честный признак «наше»: файл running_app пишем мы и только мы, в
    свой каталог данных. Ни имя процесса, ни командная строка такой гарантии не
    дают — их приходится угадывать, и на сервере они угадывались неверно.
    """
    try:
        import single_instance
        info = single_instance.running_info(config.DATA_DIR)
        pid = int(info.get('pid') or 0)
    except Exception:                              # noqa: BLE001
        return 0
    return pid if pid and pid != os.getpid() else 0


def is_our_bot(holder, port=None):
    """
    Это наша же прежняя копия, а не посторонняя программа?

    СНАЧАЛА СПРАШИВАЕМ У ПОРТА, потом сверяем PID с записанным, и только потом
    смотрим на имя процесса. Опознание по имени оставлено последним: оно
    работает, когда приложение уже не отвечает по сети, но ещё держит сокет, —
    и ошибается чаще всех остальных.
    """
    if port and asks_the_port(port):
        return True
    holder = holder or {}
    recorded = our_recorded_pid()
    if recorded and str(holder.get('pid') or '') == str(recorded):
        return True
    name = (holder.get('name') or '').lower()
    cmd = (holder.get('cmd') or '').lower()
    if name == 'kraken.exe':
        return True
    if name in ('python.exe', 'pythonw.exe'):
        # Пустая командная строка — не приговор: на свежих Windows её просто
        # нечем добыть, wmic оттуда удалён. Раз порт отвечать перестал, а
        # процесс питоновский и наш каталог рядом — считаем своим.
        if not cmd:
            return True
        return ('desktop.py' in cmd or 'bot.py' in cmd
                or 'kraken' in cmd)
    return False


def stop_holder(holder, port, wait=25):
    """
    Закрывает процесс, держащий порт, и ждёт освобождения порта.

    Ждать обязательно: система отпускает сокет не мгновенно, и запуск сразу
    после завершения процесса упёрся бы в тот же занятый порт — то есть
    выглядел бы как «не помогло».

    ЕСЛИ ДЕРЖАТЕЛЬ НЕ НАЗВАН — это НЕ повод сдаваться, и раньше было наоборот.
    Пустой словарь приходит, когда netstat не разобран: он бывает медленным,
    его вывод меняется от системы к системе, а разбор шёл по одной строке
    формата. Дальше holder['pid'] поднимал KeyError, тот молча становился
    False, и человек читал «снимите вручную». Теперь берём PID, который
    прежняя копия записала о себе, а если и его нет — просто ждём: порт может
    освободиться сам, процесс мог уже уходить.
    """
    import time

    # СВОБОДНЫЙ ПОРТ ЗАКРЫВАТЬ НЕ У КОГО, и эта проверка не формальность.
    #
    # Ниже стоит запасной путь: держателя не назвали — берём PID, который
    # работающая копия записала о себе. Без проверки порта этот путь снимал
    # приложение ВСЕГДА, даже когда снимать было нечего. Поймано на себе самым
    # неприятным способом: прогон тестов раз за разом убивал работающее
    # приложение — четыре падения подряд с кодом 1 и без единой строки в
    # журнале, потому что taskkill /F не оставляет следов.
    if not port_busy(port):
        return True

    pid = (holder or {}).get('pid') or our_recorded_pid()
    if pid:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/F', '/T'],
                           capture_output=True, timeout=20, creationflags=flags)
        except Exception as exc:                   # noqa: BLE001
            log(f'не удалось закрыть процесс {pid}: {exc}')
    else:
        log('держателя порта назвать нечем — жду, не освободится ли сам')
    for _ in range(max(1, wait)):
        if not port_busy(port):
            return True
        time.sleep(1)
    return not port_busy(port)


def free_port(preferred, tries=40):
    """
    Ближайший свободный порт, начиная с желаемого. 0 — не нашлось.

    ПОСЛЕДНЯЯ ЛИНИЯ ОБОРОНЫ, и появилась она не от хорошей жизни. Любой отказ
    в опознании держателя раньше заканчивался одинаково: «освободите порт или
    задайте другой в DASHBOARD_PORT» — то есть работой для человека вместо
    работы программы. Между «бот не запустился» и «бот работает, но на
    соседнем порту» выбор очевиден: номер порта — деталь устройства, окно
    открывается по тому адресу, который выбран, а торговля идёт.
    """
    for step in range(max(1, tries)):
        candidate = preferred + step
        if candidate > 65535:
            break
        if not port_busy(candidate):
            return candidate
    return 0


CRASH_LOG = os.path.join(config.DATA_DIR, 'crash.log')


def _remember_crashes():
    """
    Записывает причину падения на диск. Без этого её просто нет.

    ПОЧЕМУ ЭТО ПОНАДОБИЛОСЬ. Приложение трижды завершалось само по себе с кодом
    1, и в выводе не оставалось ни строчки: консоли у оконного режима нет,
    стандартный обработчик печатает разбор в поток, которого никто не читает.
    Разбирать такое можно только гаданием — а гадание на живых деньгах плохой
    способ работы.

    Ставится ДВА обработчика, и второй не менее важен первого: исключение в
    фоновом потоке главный обработчик не видит вовсе, а вся торговля у нас
    идёт именно в потоках.
    """
    import threading
    import traceback

    def write(kind, exc_type, exc, tb):
        try:
            with open(CRASH_LOG, 'a', encoding='utf-8') as fh:
                fh.write(f"\n=== {kind} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                fh.write(''.join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:                          # noqa: BLE001
            pass
        try:
            log(f'❌ {kind}: {exc_type.__name__}: {str(exc)[:160]}')
        except Exception:                          # noqa: BLE001
            pass

    def on_main(exc_type, exc, tb):
        write('падение главного потока', exc_type, exc, tb)
        sys.__excepthook__(exc_type, exc, tb)

    def on_thread(args):
        write(f'падение потока {args.thread.name if args.thread else "?"}',
              args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = on_main
    threading.excepthook = on_thread


def main():
    _remember_crashes()
    if '--selftest' in sys.argv:
        sys.exit(selftest())
    if '--diag' in sys.argv:
        sys.exit(diag())

    # Второй экземпляр не запускаем. Проверено: два щелчка дают два бота на
    # одних файлах состояния, и они затирают работу друг друга.
    import single_instance
    import updater_app
    my_version = updater_app.current_version() or 'без версии'

    after_update = '--after-update' in sys.argv
    got_lock = single_instance.acquire(config.DATA_DIR)

    # ЗАМОК ТОЖЕ НАДО УМЕТЬ ЗАБИРАТЬ, и это оказалось важнее порта. Прежняя
    # копия может ещё держать замок, УЖЕ отпустив порт: окно закрыто, сервер
    # остановлен, процесс доживает последние секунды. Запуск после подмены
    # файла упирался ровно сюда, до проверки порта не доходил вовсе и говорил
    # «Программа уже работает» — про копию, которой через секунду не станет.
    if not got_lock and after_update:
        log('после обновления: замок держит прежняя копия — закрываю её')
        got_lock = single_instance.close_previous(config.DATA_DIR)
        if not got_lock:
            log('замок отобрать не удалось')

    if not got_lock:
        # СКАЗАТЬ ОБЯЗАТЕЛЬНО, даже если чужое окно удалось поднять. Раньше
        # здесь был молчаливый выход: окно работающего экземпляра всплывало,
        # и человек, только что скачавший новую версию, видел открывшееся
        # окно СТАРОЙ и был уверен, что обновился. Так и случилось.
        info = single_instance.running_info(config.DATA_DIR)
        running = info.get('version') or 'неизвестной версии'
        where = info.get('exe') or 'путь неизвестен'
        same = str(running) == str(my_version)

        if same:
            single_instance.focus_existing(APP_TITLE)
            _alert(APP_TITLE,
                   f'Программа уже работает — версия {running}.\n'
                   f'Файл: {where}\n\n'
                   'Это та же версия, что вы сейчас запустили.\n'
                   'Окно работающей программы поднято.')
            return

        # ВЕРСИЯ ДРУГАЯ — ПРЕДЛАГАЕМ ПЕРЕЙТИ, А НЕ ОТПРАВЛЯЕМ В ДИСПЕТЧЕР
        # ЗАДАЧ. Здесь стоял совет «снимите Kraken.exe вручную», и человек
        # ровно это и делал — каждый раз. Закрыть прежнюю копию мы умеем сами
        # и по её же записанному PID; спросить перед этим обязательно, потому
        # что она может вести позиции.
        if _ask(APP_TITLE,
                f'Работает версия {running}, а вы запустили {my_version}.\n'
                f'Файл: {where}\n\n'
                'Двум копиям нельзя работать с одними данными: они затрут\n'
                'журнал сделок друг друга.\n\n'
                'Закрыть работающую версию и запустить эту?'):
            got_lock = single_instance.close_previous(config.DATA_DIR)
            if got_lock:
                log(f'прежняя копия {running} закрыта, продолжаю запуск {my_version}')

        if not got_lock:
            single_instance.focus_existing(APP_TITLE)
            _alert(APP_TITLE,
                   f'Программа уже работает — версия {running}.\n'
                   f'Файл: {where}\n\n'
                   f'ВЫ ЗАПУСТИЛИ ВЕРСИЮ {my_version}, И ОНА НЕ ЗАПУСТИЛАСЬ.\n\n'
                   'Закройте работающую программу (или снимите её в\n'
                   'диспетчере задач → Подробности) и запустите снова.')
            return

    # Порт проверяем ДО запуска бота. Замок на второй экземпляр ловит только
    # другую копию ЭТОГО приложения; бота, запущенного из исходников, он не
    # видит вовсе — у того свой процесс и свой замок или его нет совсем. А
    # порт один на всех, и именно он выдаёт чужого.
    if port_busy(config.DASHBOARD_PORT):
        holder = port_holder(config.DASHBOARD_PORT)
        who = port_owner(config.DASHBOARD_PORT) or 'неизвестно кем'
        busy = f'Порт {config.DASHBOARD_PORT} занят: {who}.'
        ours = is_our_bot(holder, config.DASHBOARD_PORT)

        if not ours:
            # Чужую программу не трогаем: порт мог занять кто угодно, и
            # снимать посторонний процесс из-за нашего запуска нельзя. Но и
            # отказываться работать из-за этого больше не станем — уйдём на
            # соседний порт. Раньше здесь стоял выход, и человек оставался с
            # советом «освободите порт»: программа не запускалась вовсе.
            freed = False
        elif after_update:
            # ЭТО НАША ЖЕ ПРЕЖНЯЯ КОПИЯ. После обновления закрываем её молча:
            # человек нажал «Обновить» и ждёт новую версию, а не диалог.
            log(f'после обновления: закрываю прежнюю копию {who}')
            freed = stop_holder(holder, config.DASHBOARD_PORT)
        else:
            freed = _ask(APP_TITLE, f'Похоже, бот уже работает: {who}.' + '\n\nДвум копиям нельзя вести одни и те же позиции: они затрут\nжурнал сделок друг друга.\n\nЗакрыть прежнюю копию и запустить эту версию?')
            freed = freed and stop_holder(holder, config.DASHBOARD_PORT)

        if freed:
            log('порт освобождён, продолжаю запуск')
        else:
            # ПОРТ ОТДАВАТЬ НЕКОМУ — БЕРЁМ ДРУГОЙ. Это и есть ответ на «руками
            # снимаю процесс в диспетчере задач»: у бота не должно быть
            # состояния, из которого его вытаскивает человек. Номер порта —
            # деталь устройства, окно откроется по выбранному адресу, торговля
            # пойдёт. Данные при этом общие с прежней копией, поэтому если она
            # ЖИВА и ведёт позиции — вот тогда останавливаемся: две копии на
            # одних файлах затрут работу друг друга, и это хуже, чем незапуск.
            moved = 0 if ours else free_port(config.DASHBOARD_PORT + 1)
            if not moved:
                _alert(APP_TITLE, busy + '\n\n' + (
                    'Прежнюю копию закрыть не удалось — снимите её вручную\n'
                    '(диспетчер задач → Подробности) и запустите снова.'
                    if ours else
                    'Это не наш бот, закрывать его я не стану, а свободного\n'
                    'порта рядом не нашлось. Освободите порт или задайте\n'
                    'другой в DASHBOARD_PORT.'))
                return
            log(f'порт {config.DASHBOARD_PORT} занят посторонним ({who}) — '
                f'перехожу на {moved}')
            os.environ['DASHBOARD_PORT'] = str(moved)
            config.DASHBOARD_PORT = moved
            _alert(APP_TITLE,
                   busy + '\n\nЭто не наш бот, и закрывать его я не стану.\n\n'
                   f'Панель открою на соседнем порту {moved} — бот работает,\n'
                   'ничего делать не нужно. Чтобы закрепить этот номер,\n'
                   f'добавьте в .env строку DASHBOARD_PORT={moved}.')

    single_instance.mark_running(config.DATA_DIR, my_version,
                                 os.path.abspath(sys.executable))

    # Ключей нет — спрашиваем окном. Без них бот падает на первом обращении к
    # бирже, а человек видит только «дашборд не поднялся»: причина настоящая,
    # но по сообщению не угадывается.
    import first_run
    if first_run.needs_setup():
        if not first_run.run_setup():
            log('Настройка не завершена — выходим')
            return

    # Боевой режим требует подтверждения. Раньше окно его просто не пускало и
    # отправляло в консоль — приложение оказывалось недоделанным ровно там, где
    # важнее всего. Требование при этом осталось прежним: набрать YES руками,
    # чтобы реальные деньги не начали торговаться по двойному щелчку.
    if config.TRADING_MODE == 'LIVE':
        if not first_run.confirm_live():
            log('Запуск в LIVE отменён')
            return
        os.environ['LIVE_CONFIRMED'] = 'YES'
        config.LIVE_CONFIRMED = 'YES'

    url = f'http://127.0.0.1:{config.DASHBOARD_PORT}/'
    dashboard.set_status('starting')

    threading.Thread(target=_run_bot, daemon=True, name='bot').start()

    # ЛЁГКАЯ СТРАНИЦА, А НЕ ТЯЖЁЛАЯ: замер на запуске дал 13 секунд против 49
    # у api/data при пороге в 40. Вопрос здесь один — поднялся ли сервер.
    if not _wait_for_dashboard(url + 'api/whoami'):
        log(f'Дашборд не ответил за {STARTUP_TIMEOUT} с — окно не открываю')
        _alert(APP_TITLE,
               f'Дашборд не поднялся за {STARTUP_TIMEOUT} секунд.\n\n'
               f'Чаще всего порт {config.DASHBOARD_PORT} занят другой программой\n'
               f'или уже запущенной копией бота.\n\n'
               f'Подробности — в файле bot_log.txt рядом с данными бота.')
        return

    _open_window(url)
    _shutdown()


if __name__ == '__main__':
    main()
