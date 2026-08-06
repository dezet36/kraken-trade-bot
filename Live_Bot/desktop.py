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
STARTUP_TIMEOUT = 40               # сколько ждём ответа дашборда, секунд

CHROME_PATHS = (
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
)


# ── Диалоги (консоли нет, сообщать об ошибке больше нечем) ───────────────────

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
    """Ждёт, пока дашборд начнёт отвечать: окно нельзя открывать раньше."""
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


def _open_window(url):
    """Открывает интерфейс лучшим доступным способом и ждёт его закрытия."""
    if _open_native(url):
        return

    process = _open_app_window(url)
    if process is not None:
        process.wait()
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
               'webview', 'pandas', 'numpy')
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


def main():
    if '--selftest' in sys.argv:
        sys.exit(selftest())
    if '--diag' in sys.argv:
        sys.exit(diag())

    # Второй экземпляр не запускаем. Проверено: два щелчка дают два бота на
    # одних файлах состояния, и они затирают работу друг друга.
    import single_instance
    import updater_app
    my_version = updater_app.current_version() or 'без версии'

    if not single_instance.acquire(config.DATA_DIR):
        # СКАЗАТЬ ОБЯЗАТЕЛЬНО, даже если чужое окно удалось поднять. Раньше
        # здесь был молчаливый выход: окно работающего экземпляра всплывало,
        # и человек, только что скачавший новую версию, видел открывшееся
        # окно СТАРОЙ и был уверен, что обновился. Так и случилось.
        info = single_instance.running_info(config.DATA_DIR)
        running = info.get('version') or 'неизвестной версии'
        where = info.get('exe') or 'путь неизвестен'
        single_instance.focus_existing(APP_TITLE)
        same = str(running) == str(my_version)
        _alert(APP_TITLE,
               f'Программа уже работает — версия {running}.\n'
               f'Файл: {where}\n\n'
               + ('Это та же версия, что вы сейчас запустили.\n'
                  'Окно работающей программы поднято.'
                  if same else
                  f'ВЫ ЗАПУСТИЛИ ВЕРСИЮ {my_version}, И ОНА НЕ ЗАПУСТИЛАСЬ.\n'
                  'Двум копиям нельзя работать с одними данными: они затрут\n'
                  'журнал сделок друг друга.\n\n'
                  'Чтобы перейти на новую версию: закройте работающую\n'
                  'программу (или снимите Kraken.exe в диспетчере задач)\n'
                  'и запустите новый файл снова.'))
        return

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

    if not _wait_for_dashboard(url + 'api/data'):
        _alert(APP_TITLE,
               f'Дашборд не поднялся за {STARTUP_TIMEOUT} секунд.\n\n'
               f'Чаще всего порт {config.DASHBOARD_PORT} занят другой программой\n'
               f'или уже запущенной копией бота.\n\n'
               f'Подробности — в файле bot_log.txt рядом с данными бота.')
        return

    _open_window(url)
    log('Окно приложения закрыто — бот остановлен')


if __name__ == '__main__':
    main()
