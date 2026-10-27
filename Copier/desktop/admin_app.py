"""
CopierAdmin.exe — окно админки копировщика на Windows.

Как KrakenRemote у торгового бота (control/remote_app.py), но своё и отдельное:
  1. настройки сервера (tkinter) — адрес, пользователь, порт SSH, файл ключа;
  2. токен админки читается с сервера по ssh (на этой машине не хранится);
  3. туннель 127.0.0.1:8798 → сервер 127.0.0.1:8790;
  4. окно Chrome/Edge в режиме приложения со своим профилем;
  5. сторож переподнимает туннель при обрыве или зависании, пока окно открыто.
"""

import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import socket

try:
    from desktop import tunnel
except ImportError:                                    # запуск из папки desktop или из exe
    import tunnel

LOG_NAME = 'copier_admin.log'
FIELDS = (('host', 'Адрес сервера', 'IP или имя'),
          ('user', 'Пользователь', 'обычно root'),
          ('ssh_port', 'Порт SSH', '22'),
          ('key', 'Файл ключа SSH', 'пусто — ключ по умолчанию'),
          ('remote_port', 'Порт админки', '8790'))
DEFAULTS = {'user': 'root', 'ssh_port': '22', 'remote_port': '8790'}
CHROME_PATHS = (
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    os.path.join(os.environ.get('LOCALAPPDATA', ''), r'Google\Chrome\Application\chrome.exe'),
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
)
PROBE_EVERY = 15
PROBE_FAILS = 3


def log(message):
    path = os.path.join(tunnel.base_dir(), LOG_NAME)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + '.1')
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def fail(message):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'CopierAdmin', 0x10)
    except Exception:                                  # noqa: BLE001
        print(message)


def ask_settings(current, error=''):
    try:
        import tkinter as tk
        from tkinter import filedialog, ttk
    except ImportError:
        return None
    root = tk.Tk()
    root.title('Админка копировщика — сервер')
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()
    ttk.Label(frame, text='Куда подключаться', font=('Segoe UI', 11, 'bold')).grid(
        row=0, column=0, columnspan=3, sticky='w')
    ttk.Label(frame, text='SSH тем же ключом, каким вы заходите на сервер.\n'
                          'Пароли и токен админки здесь не хранятся.',
              foreground='#555').grid(row=1, column=0, columnspan=3, sticky='w', pady=(0, 12))
    entries = {}
    for index, (name, label, hint) in enumerate(FIELDS, start=2):
        ttk.Label(frame, text=label).grid(row=index, column=0, sticky='w', padx=(0, 10), pady=4)
        field = ttk.Entry(frame, width=38)
        field.insert(0, str(current.get(name) or DEFAULTS.get(name, '')))
        field.grid(row=index, column=1, sticky='w', pady=4)
        entries[name] = field
        if name == 'key':
            def browse(f=field):
                path = filedialog.askopenfilename(title='Ключ SSH')
                if path:
                    f.delete(0, 'end')
                    f.insert(0, path)
            ttk.Button(frame, text='…', width=3, command=browse).grid(row=index, column=2, padx=(6, 0))
        else:
            ttk.Label(frame, text=hint, foreground='#888').grid(row=index, column=2, sticky='w', padx=(6, 0))
    status = ttk.Label(frame, text=error, foreground='#b00020', wraplength=460, justify='left')
    status.grid(row=20, column=0, columnspan=3, sticky='w', pady=(12, 0))
    result = {}

    def collect():
        return {name: entries[name].get().strip() for name in entries}

    def check():
        status.configure(text='Проверяю…', foreground='#555')
        root.update_idletasks()
        process, problem = tunnel.open_tunnel(collect())
        if process:
            process.terminate()
            token, why = tunnel.fetch_token(collect())
            status.configure(text='Соединение работает.' if token else f'Туннель есть, но токена нет: {why}',
                             foreground='#1a7f43' if token else '#b00020')
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
    ttk.Button(buttons, text='Проверить', command=check).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text='Подключиться', command=save).grid(row=0, column=1)
    root.bind('<Return>', lambda _e: save())
    root.eval('tk::PlaceWindow . center')
    root.mainloop()
    return result or None


def profile_dir():
    path = os.path.join(tunnel.base_dir(), 'copier_admin_profile')
    os.makedirs(path, exist_ok=True)
    return path


def screen_width():
    try:
        import ctypes
        return ctypes.windll.user32.GetSystemMetrics(0)
    except Exception:                                  # noqa: BLE001
        return 0


def open_app_window(link):
    """Окно браузера в режиме приложения со своим профилем (иначе подклеится к открытому браузеру)."""
    browser = next((p for p in CHROME_PATHS if p and os.path.exists(p)), None)
    if not browser:
        return None
    # Таблицы админки широкие: на экране уже 1500 точек — во весь экран
    # (у владельца 1280×1024, окно 1400 не помещалось).
    size = ['--start-maximized'] if screen_width() < 1500 else ['--window-size=1400,900']
    return subprocess.Popen([browser, f'--app={link}', f'--user-data-dir={profile_dir()}', *size,
                             '--no-first-run', '--no-default-browser-check'])


def profile_pids():
    """PID браузеров с НАШИМ профилем — окно могло переехать в другой процесс (обновление Chrome)."""
    profile = profile_dir().lower()
    pids = []
    try:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        found = subprocess.run(['wmic', 'process', 'where', "name='chrome.exe' or name='msedge.exe'",
                                'get', 'ProcessId,CommandLine', '/format:csv'],
                               capture_output=True, timeout=20, creationflags=flags)
        for line in found.stdout.decode('utf-8', 'replace').splitlines():
            if profile in line.lower():
                pid = line.rstrip().rsplit(',', 1)[-1].strip()
                if pid.isdigit():
                    pids.append(pid)
    except Exception:                                  # noqa: BLE001
        pass
    return pids


def wait_window(window, period=5.0):
    """Держим туннель, пока жив хоть один браузер с нашим профилем (урок KrakenRemote, 18.10.2026)."""
    window.wait()
    while profile_pids():
        time.sleep(period)


def tunnel_hung(timeout=5.0):
    """Соединение принято, а ответа нет — признак зависшего ssh (а не лежащей админки)."""
    try:
        with urllib.request.urlopen(tunnel.url('login'), timeout=timeout):
            return False
    except socket.timeout:
        return True
    except urllib.error.URLError as error:
        return isinstance(error.reason, socket.timeout)
    except Exception:                                  # noqa: BLE001
        return False


def keep_alive(cfg, link):
    delay = 3
    while not link['closed']:
        fails = 0
        reason = ''
        while not link['closed']:
            process = link['process']
            if process is not None and process.poll() is not None:
                said = ' | '.join(getattr(process, 'stderr_tail', None) or ())
                reason = 'разорвано' + (f' (ssh: {said[-300:]})' if said else '')
                break
            if tunnel_hung():
                fails += 1
                if fails >= PROBE_FAILS:
                    try:
                        process.terminate()
                    except Exception:                  # noqa: BLE001
                        pass
                    reason = 'зависло'
                    break
            else:
                fails = 0
            time.sleep(PROBE_EVERY)
        if link['closed']:
            return
        log(f'Соединение {reason} — переподключаюсь')
        while not link['closed']:
            time.sleep(delay)
            process, error = tunnel.open_tunnel(cfg)
            if process:
                link['process'] = process
                log('Соединение восстановлено')
                delay = 3
                break
            log(f'Не удалось: {error}')
            delay = min(delay * 2, 60)


def claim_single_instance():
    if sys.platform != 'win32':
        return True
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\CopierAdmin-single-instance')
        claim_single_instance.handle = handle
        return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS
    except Exception:                                  # noqa: BLE001
        return True


def main():
    if not claim_single_instance():
        fail('CopierAdmin уже запущен — окно открыто.')
        return 0
    cfg = tunnel.load_settings()
    error = ''
    while True:
        if not cfg.get('host') or error:
            cfg = ask_settings(cfg, error)
            if not cfg:
                return 1
            tunnel.save_settings(cfg)
        process, error = tunnel.open_tunnel(cfg)
        if not process:
            continue
        token, why = tunnel.fetch_token(cfg)
        if token:
            break
        process.terminate()
        error = f'Туннель поднят, но токен админки на сервере не прочитан: {why}'
    log(f'Туннель поднят: {tunnel.target(cfg)}')
    link = {'process': process, 'closed': False}
    threading.Thread(target=keep_alive, args=(cfg, link), daemon=True).start()
    try:
        window = open_app_window(tunnel.url(f'login?token={token}'))
        if window is None:
            import webbrowser
            webbrowser.open(tunnel.url(f'login?token={token}'))
            fail('Chrome и Edge не найдены — админка открыта в браузере по умолчанию.\n\n'
                 'Соединение держит эта программа: закройте её, когда закончите.')
            while True:
                time.sleep(3600)
        wait_window(window)
    except KeyboardInterrupt:
        pass
    finally:
        link['closed'] = True
        try:
            link['process'].terminate()
        except Exception:                              # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
