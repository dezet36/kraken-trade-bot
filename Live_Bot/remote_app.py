"""
Приложение «Kraken — сервер»: окно панели, подключённое к боту на сервере.

ЧТО ОНО ДЕЛАЕТ. Поднимает туннель SSH до сервера, пробрасывает порт панели к
себе на петлевой адрес и открывает его своим окном. Для панели запрос приходит
с localhost, и она не знает, что человек за тысячу километров: работают и
просмотр, и управление.

ПОЧЕМУ ЭТО ОТДЕЛЬНАЯ ПРОГРАММА, А НЕ РЕЖИМ desktop.py. У desktop.py другая
задача: он поднимает бота на ЭТОЙ машине и показывает его. Здесь бота нет
вовсе — есть только окно и соединение. Свалив их в один файл, получили бы
развилку «локально или удалённо» в каждой второй функции, а запуск не того
режима по ошибке показывал бы чужие числа, ничем не выдав подмены.

ПОЧЕМУ TKINTER ДЛЯ НАСТРОЕК. Ровно та же причина, что и в first_run: страницу
настроек рисует панель, а панель — на сервере, к которому мы ещё не
подключились. Курица и яйцо. Tkinter входит в стандартную библиотеку и
работает до всего остального.

СБОРКА: powershell -ExecutionPolicy Bypass -File Live_Bot\\build_remote_exe.ps1
"""

import os
import subprocess
import sys
import threading
import time

import remote

WINDOW_TITLE = 'Kraken — сервер'

LOG_NAME = 'remote.log'
LOG_LIMIT = 512 * 1024


def log(message):
    """
    Пишет строку в remote.log рядом с настройками и в консоль, если она есть.

    У собранной программы консоли нет (--windowed), и всё, что сторож
    говорил через print, пропадало: «соединение разорвано — переподключаюсь»
    не видел никто. Когда окно в очередной раз показало пустоту, причину
    пришлось восстанавливать по netstat. Файл переживает и окно, и перезапуск;
    растёт до LOG_LIMIT, потом начинается заново.
    """
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")} {message}'
    try:
        print(line)
    except Exception:                              # noqa: BLE001
        pass
    path = os.path.join(os.path.dirname(remote.settings_path()), LOG_NAME)
    try:
        mode = 'a'
        if os.path.exists(path) and os.path.getsize(path) > LOG_LIMIT:
            mode = 'w'
        with open(path, mode, encoding='utf-8') as fh:
            fh.write(line + chr(10))
    except OSError:
        pass


# ── Окно настроек соединения ─────────────────────────────────────────────────

FIELDS = (
    ('host', 'Адрес сервера', 'IP или имя, например 203.0.113.10'),
    ('user', 'Пользователь', 'обычно root'),
    ('ssh_port', 'Порт SSH', 'по умолчанию 22'),
    ('key', 'Файл ключа', 'необязательно, если ключ в ~/.ssh/id_rsa'),
    ('remote_port', 'Порт панели на сервере', 'по умолчанию 8787'),
)

DEFAULTS = {'user': 'root', 'ssh_port': '22', 'remote_port': '8787'}

# Коды клавиш Windows. Привязываемся к НИМ, а не к буквам — см. enable_editing.
_KEY_A, _KEY_C, _KEY_V, _KEY_X = 65, 67, 86, 88

# Бит модификатора Ctrl в поле state события Tk.
_CTRL = 0x4


def edit_action(keycode, state):
    """
    Какое действие означает нажатие. None — обычный ввод, не наше дело.

    ОТДЕЛЬНОЙ ФУНКЦИЕЙ РАДИ ПРОВЕРЯЕМОСТИ. Решение о клавише — это единственное
    место, где легко ошибиться: перепутать код, забыть модификатор, поймать
    лишнее. А проверить его внутри привязки нельзя: Tk отказывается
    синтезировать событие с кириллическим keysym, и попытка изобразить русскую
    раскладку в проверке упирается в само тестовое окружение.

    Здесь же достаточно передать два числа.
    """
    if not state & _CTRL:
        return None
    return {_KEY_V: 'Paste', _KEY_C: 'Copy',
            _KEY_X: 'Cut', _KEY_A: 'select'}.get(keycode)


def enable_editing(widget, tk, root):
    """
    Возвращает полю вставку и копирование при любой раскладке.

    ЗАЧЕМ. Tk привязывает Ctrl+V к СИМВОЛУ «v». При русской раскладке система
    сообщает «м», привязки для неё нет, и вставка молча не происходит: человек
    жмёт Ctrl+V, ничего не видит и не понимает почему. Адрес сервера и путь к
    ключу набирать руками — то ещё удовольствие, а ошибиться в них легко.

    Привязка по КОДУ клавиши от раскладки не зависит: физическая клавиша V
    имеет код 86 независимо от того, какая буква на ней нарисована.

    Плюс правая кнопка мыши: её видно, и она работает даже когда сочетание
    перехватила другая программа.
    """
    def keyed(event):
        action = edit_action(event.keycode, event.state)
        if action is None:
            return None
        if action == 'select':
            widget.select_range(0, 'end')
            widget.icursor('end')
        else:
            widget.event_generate(f'<<{action}>>')
        # 'break' обязателен: иначе при латинской раскладке сработает и наша
        # привязка, и родная, и текст вставится дважды.
        return 'break'

    widget.bind('<KeyPress>', keyed)

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label='Вставить',
                     command=lambda: widget.event_generate('<<Paste>>'))
    menu.add_command(label='Копировать',
                     command=lambda: widget.event_generate('<<Copy>>'))
    menu.add_command(label='Вырезать',
                     command=lambda: widget.event_generate('<<Cut>>'))
    menu.add_separator()
    menu.add_command(label='Выделить всё',
                     command=lambda: widget.select_range(0, 'end'))

    def popup(event):
        widget.focus_set()
        menu.tk_popup(event.x_root, event.y_root)
        return 'break'

    widget.bind('<Button-3>', popup)


def ask_settings(current, error=''):
    """
    Окно с вопросами о сервере. Возвращает словарь или None, если закрыли.

    Проверка соединения встроена: нажав «Проверить», человек узнаёт об ошибке
    здесь, а не после того, как окно откроется пустым. Ровно тем же способом,
    каким потом пойдёт соединение, — иначе проверка проверяла бы не то.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
    except ImportError:
        print('tkinter недоступен — окно настройки показать нечем')
        return None

    root = tk.Tk()
    root.title('Подключение к серверу')
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()

    ttk.Label(frame, text='Куда подключаться',
              font=('Segoe UI', 11, 'bold')).grid(row=0, column=0, columnspan=3,
                                                  sticky='w', pady=(0, 2))
    ttk.Label(frame, text='Соединение идёт по SSH тем же ключом, каким вы\n'
                          'заходите на сервер. Пароли здесь не хранятся.',
              foreground='#555').grid(row=1, column=0, columnspan=3,
                                      sticky='w', pady=(0, 12))

    entries = {}
    for index, (name, label, hint) in enumerate(FIELDS, start=2):
        ttk.Label(frame, text=label).grid(row=index, column=0, sticky='w',
                                          padx=(0, 10), pady=4)
        field = ttk.Entry(frame, width=38)
        field.insert(0, str(current.get(name) or DEFAULTS.get(name, '')))
        field.grid(row=index, column=1, sticky='w', pady=4)
        enable_editing(field, tk, root)
        entries[name] = field
        if name == 'key':
            def pick(entry=field):
                path = filedialog.askopenfilename(title='Файл ключа SSH')
                if path:
                    entry.delete(0, 'end')
                    entry.insert(0, path)
            ttk.Button(frame, text='…', width=3, command=pick).grid(
                row=index, column=2, padx=(6, 0))
        else:
            ttk.Label(frame, text=hint, foreground='#888').grid(
                row=index, column=2, sticky='w', padx=(6, 0))

    status = ttk.Label(frame, text=error, foreground='#b00020',
                       wraplength=460, justify='left')
    status.grid(row=20, column=0, columnspan=3, sticky='w', pady=(12, 0))

    result = {}

    def collect():
        return {name: entries[name].get().strip() for name in entries}

    def check():
        status.configure(text='Проверяю…', foreground='#555')
        root.update_idletasks()
        process, problem = remote.open_tunnel(collect())
        if process:
            process.terminate()
            status.configure(text='Соединение работает.', foreground='#1a7f43')
        else:
            status.configure(text=problem, foreground='#b00020')

    def save():
        cfg = collect()
        if not cfg.get('host'):
            status.configure(text='Укажите адрес сервера.', foreground='#b00020')
            return
        result.update(cfg)
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=21, column=0, columnspan=3, sticky='e', pady=(14, 0))
    ttk.Button(buttons, text='Проверить', command=check).grid(row=0, column=0,
                                                              padx=(0, 8))
    ttk.Button(buttons, text='Подключиться', command=save).grid(row=0, column=1)

    root.bind('<Return>', lambda _event: save())
    root.eval('tk::PlaceWindow . center')
    root.mainloop()
    return result or None


# ── Окно панели ──────────────────────────────────────────────────────────────

CHROME_PATHS = (
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    os.path.join(os.environ.get('LOCALAPPDATA', ''),
                 r'Google\Chrome\Application\chrome.exe'),
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
)


def open_app_window(url):
    """
    Окно браузера в режиме приложения. None — подходящего браузера нет.

    ПОЧЕМУ ЭТОТ ПУТЬ ПЕРВЫЙ, А НЕ ЗАПАСНОЙ. На Windows 10 LTSC 2019 WebView2
    установлен, все его библиотеки в сборку попали, страница и данные с сервера
    приходят (200, 205 КБ) — а окно остаётся БЕЛЫМ. Движок не отрисовывает.

    Отличить это в коде нельзя: webview.start() не бросает исключения и не
    возвращает признака неудачи. Белое окно от рабочего программа не отличает,
    поэтому запасной путь, привязанный к исключению, не включался никогда.

    Окно Chrome в режиме приложения выглядит так же: без адресной строки и
    вкладок, со своей кнопкой на панели задач. Отдельный профиль обязателен —
    иначе окно подклеится к уже открытому браузеру, а программа
    завершится сразу после запуска.

    Порядок можно перевернуть переменной REMOTE_ENGINE=webview.
    """
    browser = next((path for path in CHROME_PATHS
                    if path and os.path.exists(path)), None)
    if not browser:
        return None

    profile = os.path.join(os.path.dirname(remote.settings_path()),
                           'window_profile')
    os.makedirs(profile, exist_ok=True)
    return subprocess.Popen([
        browser,
        f'--app={url}',
        f'--user-data-dir={profile}',
        '--window-size=1360,900',
        '--no-first-run',
        '--no-default-browser-check',
    ])


# Сколько ждём доказательства, что страница отрисовалась. Щедро намеренно: 19
# сентября 2026 после перезагрузки машины Chrome шёл к первой навигации 14
# секунд — диск в это время читает всё сразу, и нетерпеливый сторож убивал бы
# исправное окно.
PAINT_TIMEOUT = float(os.getenv('REMOTE_PAINT_TIMEOUT', 45))


def views(url, timeout=3.0):
    """
    Сколько раз панель отдавала данные странице. None — спросить не вышло.

    Это единственный доступный признак того, что окно ПОКАЗЫВАЕТ панель, а не
    просто открылось. Страницу рисует JS: пока он не запросил данные, окно
    белое, что бы ни ответил сервер на саму страницу.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url + 'api/whoami', timeout=timeout) as answer:
            return json.loads(answer.read().decode('utf-8')).get('views')
    except Exception:                              # noqa: BLE001
        return None


def painted(url, before, timeout=PAINT_TIMEOUT):
    """
    Ждёт, пока счётчик показов сдвинется. False — окно так ничего и не нарисовало.

    Сдвиг в ЛЮБУЮ сторону считается показом: если бот на сервере
    перезапустился, счётчик пойдёт с нуля, и требование «стало больше» соврало
    бы про исправное окно.

    Неизвестность (None) показом не считается: не дозвонились до панели —
    значит и страница не дозвонилась.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = views(url)
        if now is not None and now != before:
            return True
        time.sleep(1.0)
    return False


def close_windows():
    """
    Закрывает браузеры, работающие С НАШИМ профилем. Возвращает, сколько нашёл.

    Нужен для повтора: второй запуск chrome с тем же профилем открыл бы ВТОРОЕ
    окно рядом с белым, а не перерисовал первое. Обычные окна человека не
    трогаются — отбор идёт по пути профиля, который заводим мы сами.
    """
    profile = os.path.join(os.path.dirname(remote.settings_path()),
                           'window_profile')
    killed = 0
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        found = subprocess.run(
            ['wmic', 'process', 'where',
             "name='chrome.exe' or name='msedge.exe'",
             'get', 'ProcessId,CommandLine', '/format:csv'],
            capture_output=True, timeout=20, creationflags=flags)
        for line in found.stdout.decode('utf-8', 'replace').splitlines():
            if profile.lower() not in line.lower():
                continue
            pid = line.rstrip().rsplit(',', 1)[-1].strip()
            if not pid.isdigit():
                continue
            subprocess.run(['taskkill', '/F', '/PID', pid],
                           capture_output=True, timeout=20,
                           creationflags=flags)
            killed += 1
    except Exception:                              # noqa: BLE001
        return killed
    return killed


def open_native_window(url, on_close):
    """
    Своё окно через pywebview. False — открыть не удалось.

    Движок задаётся явно: edgechromium — это WebView2. Без явного указания
    pywebview может выбрать движок Internet Explorer, и панель в нём
    разъедется: там нет ни grid, ни современного CSS.
    """
    try:
        import webview
    except ImportError:
        return False

    window = webview.create_window(WINDOW_TITLE, url,
                                   width=1360, height=900, min_size=(1000, 640))
    window.events.closed += on_close
    gui = 'edgechromium' if sys.platform == 'win32' else None
    try:
        webview.start(gui=gui)          # блокирует до закрытия окна
        return True
    except Exception as exc:            # noqa: BLE001
        log(f'Своё окно не открылось ({exc})')
        return False


def open_in_browser(url):
    """Запасной путь: обычный браузер. Хуже видом, но человек увидит данные."""
    import webbrowser
    webbrowser.open(url)


def show_dashboard(url, attempts=2):
    """
    Открывает окно и УБЕЖДАЕТСЯ, что в нём видна панель. None — не вышло.

    ПОЧЕМУ ПРОВЕРКА ВООБЩЕ НУЖНА. Запуск окна успехом не является. 19 сентября
    2026 человек включил машину, запустил программу и десять минут смотрел на
    белый прямоугольник. Всё, что можно было проверить изнутри, было в
    порядке: туннель поднят, страница с сервера приходит целиком, браузер
    запущен и живёт. Сам Chrome записал причину в свой журнал событий —
    `FinishNav3 ERR_ABORTED`: навигация оборвалась. В режиме --app страницы
    ошибок нет, поэтому окно осталось просто белым.

    Изнутри такое видно ровно одним способом: спросить у панели, забирал ли
    кто-нибудь данные. Страницу рисует JS, и пока он не сходил за данными,
    окно пустое, чем бы ни ответил сервер на саму страницу.

    ПОВТОР ЗАКРЫВАЕТ ПРЕЖНЕЕ ОКНО. Второй запуск с тем же профилем открыл бы
    второе окно рядом с белым: у Chrome один профиль — один процесс, и новый
    запуск лишь просит его показать ещё одно окно.
    """
    for attempt in range(1, attempts + 1):
        before = views(url)
        window = open_app_window(url)
        if window is None:
            return None                  # браузера нет — пусть решает вызвавший
        if painted(url, before):
            return window
        log(f'Окно не показало панель (попытка {attempt} из {attempts}).')
        close_windows()
        try:
            window.terminate()
        except Exception:                          # noqa: BLE001
            pass
    return None


def fail(message):
    """Показывает отказ окном, а не строкой в консоли, которой нет у .exe."""
    log(message)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('Kraken — сервер', message)
        root.destroy()
    except Exception:                              # noqa: BLE001
        pass


# Имя события Windows, по которому вторая копия узнаёт о первой ещё до
# того, как та успела занять порт. Порт — второй признак, на случай, если
# первая копия упала, а её туннель остался жить.
SINGLE_INSTANCE_NAME = 'Local\\KrakenRemote-single-instance'

# Заголовок окна панели — по нему вторая копия находит окно первой, чтобы
# поднять его на передний план вместо второго окна.
DASHBOARD_TITLE_MARK = 'Kraken'


def claim_single_instance():
    """
    Захватывает именованный мьютекс. False — программа уже запущена.

    Держится открытым до конца процесса намеренно: закрой его — и вторая
    копия решит, что она первая.
    """
    if sys.platform != 'win32':
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_NAME)
        if not handle:
            return True
        already = kernel32.GetLastError() == 183          # ERROR_ALREADY_EXISTS
        globals()['_single_instance_handle'] = handle     # не дать закрыться
        return not already
    except Exception:                                  # noqa: BLE001
        return True


def focus_existing_window():
    """
    Поднимает уже открытое окно панели на передний план. False — не нашли.

    Ищется по заголовку среди видимых окон: у окна Chrome в режиме
    приложения заголовок — это <title> страницы панели.
    """
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def walk(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if DASHBOARD_TITLE_MARK in title and title != WINDOW_TITLE + ' — настройки':
                found.append(hwnd)
            return True

        user32.EnumWindows(walk, 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, 9)                    # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        return bool(found)
    except Exception:                                  # noqa: BLE001
        return False


def tunnel_already_up():
    """
    Живой туннель без живой программы: осиротевший ssh после падения.

    Порт занят и за ним отвечает НАША панель (api/whoami). Такой туннель
    используется, а не оспаривается: иначе программу нельзя было бы запустить
    до перезагрузки машины.
    """
    return remote.port_open(remote.LOCAL_PORT) and views(remote.url()) is not None


# Как часто сторож щупает туннель и сколько подряд неответов считает зависанием.
# 3 × (15 с + 5 с ожидания) — около минуты: столько окно показывает старые
# числа, прежде чем туннель пересоберут. Одиночный неответ не в счёт — он
# бывает, когда сервер занят разбором модели.
PROBE_EVERY = float(os.getenv('REMOTE_PROBE_EVERY', 15))
PROBE_FAILS = int(os.getenv('REMOTE_PROBE_FAILS', 3))


def tunnel_hung(url, timeout=5.0):
    """
    True, когда туннель ПРИНИМАЕТ соединение, но ответа через него нет.

    ЭТО ДРУГОЙ ПРИЗНАК, ЧЕМ «ПАНЕЛЬ НЕ ОТВЕТИЛА». Если панель на сервере
    лежит (бот перезапускается), ssh закрывает канал сразу — соединение
    сбрасывается за миллисекунды, и туннель тут ни при чём: перезапускать
    его было бы вредно, он поднимется, а панель — нет. Зависший же ssh
    (см. remote.drain_stderr) соединение принимает — порт слушает система, —
    а дальше молчит: запрос висит до истечения времени. Только это и есть
    признак мёртвого туннеля при живом процессе.
    """
    import socket
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url + 'api/whoami', timeout=timeout):
            return False
    except socket.timeout:
        return True
    except urllib.error.URLError as error:
        return isinstance(error.reason, socket.timeout)
    except Exception:                              # noqa: BLE001
        return False                               # сброс, отказ, HTTP-ошибка — ссылка жива


def watch_tunnel(link, sleep=time.sleep, hung=None):
    """
    Ждёт, пока туннель умрёт или зависнет. Возвращает причину словами.

    Два признака вместо одного. Первая версия ждала только смерти ssh
    (process.wait()) — и не увидела туннель, который восемь часов стоял с
    живым процессом и живым соединением, не пропуская ни одного запроса.
    Теперь раз в PROBE_EVERY секунд сторож сам просит у панели ответ; если
    соединение принято, а ответа нет PROBE_FAILS раз подряд — ssh снимается
    и поднимается заново, как после обрыва.
    """
    hung = hung or (lambda: tunnel_hung(remote.url()))
    fails = 0
    while not link['closed']:
        process = link['process']
        if process is not None:
            if process.poll() is not None:
                said = ' | '.join(getattr(process, 'stderr_tail', None) or ())
                return 'разорвано' + (f' (ssh: {said[-300:]})' if said else '')
        elif not remote.port_open(remote.LOCAL_PORT):
            # Чужой (осиротевший) туннель: процесса у нас нет, следим за портом.
            return 'разорвано'
        if hung():
            fails += 1
            if fails >= PROBE_FAILS:
                if process is not None:
                    try:
                        process.terminate()
                    except Exception:              # noqa: BLE001
                        pass
                return f'зависло: туннель принимает соединения, но не отвечает {fails} раза подряд'
        else:
            fails = 0
        sleep(PROBE_EVERY)
    return ''


def keep_alive(cfg, link, sleep=time.sleep, log=log, hung=None):
    """
    Держит туннель живым, пока окно открыто.

    Ждёт смерти или зависания ssh (watch_tunnel), потом поднимает его заново
    с нарастающей паузой (3 с → 60 с): сервер мог перезагружаться, сеть —
    пропасть на минуту. Останавливается, когда программа закрывается
    (link['closed']).
    """
    delay = 3
    while not link['closed']:
        reason = watch_tunnel(link, sleep=sleep, hung=hung)
        if link['closed']:
            return
        log(f'Соединение с сервером {reason} — переподключаюсь.')
        while not link['closed']:
            sleep(delay)
            if link['closed']:
                return
            process, error = remote.open_tunnel(cfg)
            if process:
                link['process'] = process
                log('Соединение восстановлено.')
                delay = 3
                break
            log(f'Не удалось: {error}')
            delay = min(delay * 2, 60)


def main():
    # ОДНА КОПИЯ, И ТОЛЬКО ОДНА. Второй запуск — по ярлыку, из панели задач,
    # по привычке — не открывает второе окно и не спорит за порт, а поднимает
    # уже открытое окно и уходит. Если окно не нашлось (первая копия ещё
    # поднимает туннель или окно свёрнуто в другой рабочий стол), несколько
    # секунд пробуем снова и выходим молча: программа уже работает.
    if not claim_single_instance():
        for _ in range(10):
            if focus_existing_window():
                break
            time.sleep(1)
        return 0

    cfg = remote.load_settings()
    error = ''
    process = None
    if tunnel_already_up():
        # Осиротевший туннель прошлой копии: используем, ssh не поднимаем.
        # Сторож ниже следит за портом и переподключится, если он закроется.
        log('Туннель уже поднят — использую его.')
    else:
        # Спрашиваем, пока не получим рабочие настройки или пока не закроют
        # окно. Один проход был бы хуже: ошибся в адресе — и запускай заново.
        while True:
            if not cfg.get('host') or error:
                cfg = ask_settings(cfg, error)
                if not cfg:
                    return 1
                remote.save_settings(cfg)
            process, error = remote.open_tunnel(cfg)
            if process:
                log(f'Туннель поднят: {cfg.get("user") or "root"}@{cfg.get("host")}')
                break

    link = {'process': process, 'closed': False}

    def shutdown():
        # Туннель НЕ демон и переживёт окно, заняв порт до перезагрузки.
        link['closed'] = True
        try:
            if link['process'] is not None:
                link['process'].terminate()
        except Exception:                          # noqa: BLE001
            pass

    # СТОРОЖ ПЕРЕПОДКЛЮЧАЕТ, А НЕ ТОЛЬКО СООБЩАЕТ. Первая версия печатала
    # «соединение разорвано» в консоль, которой у окна нет, и окно жило
    # дальше без связи: вкладки показывали последние удачные числа, а вкладка,
    # открытая после обрыва, — пустоту. 19 сентября 2026 так и выглядел
    # «пустой раздел ИИ»: ssh давно умер, а страница не знала об этом.
    # Пока туннель лежит, страница честно пишет «нет связи»; как только он
    # поднят, её опросы раз в несколько секунд оживают сами.
    threading.Thread(target=keep_alive, args=(cfg, link), daemon=True).start()

    # ПОРЯДОК ВЫБРАН ПО ЗАМЕРУ, А НЕ ПО ВКУСУ. На Windows 10 LTSC 2019
    # WebView2 установлен, библиотеки в сборке есть, страница с сервера
    # приходит — а окно остаётся белым. Отличить это в коде нельзя:
    # webview.start() не бросает исключения и не сообщает о неудаче.
    # Поэтому первым идёт путь, который проверенно работает.
    engine = os.getenv('REMOTE_ENGINE', 'chrome').lower()
    try:
        window = None
        if engine != 'webview':
            window = show_dashboard(remote.url())
        if window is not None:
            window.wait()               # держим туннель, пока открыто окно
        elif not open_native_window(remote.url(), lambda: shutdown()):
            open_in_browser(remote.url())
            # ОТКАЗ ГОВОРИТСЯ ВСЛУХ. Прежде запасной путь срабатывал молча, и
            # человек оставался с белым окном, не зная ни что оно не своё, ни
            # что данные открылись в соседней вкладке. Молчаливый запасной
            # путь неотличим от поломки.
            fail('Своё окно не показало панель, поэтому она открыта в вашем '
                 'браузере.\n\nСоединение с сервером держит эта программа — '
                 'закройте её, когда закончите. Вкладка с панелью при этом '
                 'перестанет обновляться.')
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    finally:
        shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
