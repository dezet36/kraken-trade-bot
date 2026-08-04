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

def _open_native(url):
    """Нативное окно через pywebview. None — пакет не установлен."""
    try:
        import webview
    except ImportError:
        return None
    window = webview.create_window(APP_TITLE, url, width=1280, height=860,
                                   min_size=(900, 600))
    webview.start()          # блокирует до закрытия окна
    return window


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
    if _open_native(url) is not None:
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

def main():
    # Боевой режим требует двойного подтверждения с клавиатуры, а у окна нет
    # ввода. Молча пропустить подтверждение нельзя: оно и существует затем,
    # чтобы реальные деньги не начали торговаться по двойному клику.
    if config.TRADING_MODE == 'LIVE':
        _alert(APP_TITLE,
               'Режим LIVE (реальные деньги) из окна не запускается.\n\n'
               'Подтверждение запуска требует ввода с клавиатуры, поэтому\n'
               'запусти бота из консоли: start.bat\n\n'
               'Для фантомной торговли поставь в .env  TRADING_MODE=PAPER')
        return

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
