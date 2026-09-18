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
import sys
import threading

import remote

WINDOW_TITLE = 'Kraken — сервер'


# ── Окно настроек соединения ─────────────────────────────────────────────────

FIELDS = (
    ('host', 'Адрес сервера', 'IP или имя, например 203.0.113.10'),
    ('user', 'Пользователь', 'обычно root'),
    ('ssh_port', 'Порт SSH', 'по умолчанию 22'),
    ('key', 'Файл ключа', 'необязательно, если ключ в ~/.ssh/id_rsa'),
    ('remote_port', 'Порт панели на сервере', 'по умолчанию 8787'),
)

DEFAULTS = {'user': 'root', 'ssh_port': '22', 'remote_port': '8787'}


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

def open_window(url, on_close):
    """
    Окно панели. False — открыть не удалось.

    Движок задаётся явно: edgechromium — это WebView2, встроенный в Windows 10
    и новее. Без явного указания pywebview может выбрать движок Internet
    Explorer, и панель в нём разъедется: там нет ни grid, ни современного CSS.
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
        print(f'Своё окно не открылось ({exc}) — открою в браузере')
        return False


def open_in_browser(url):
    """Запасной путь: обычный браузер. Хуже видом, но человек увидит данные."""
    import webbrowser
    webbrowser.open(url)


def fail(message):
    """Показывает отказ окном, а не строкой в консоли, которой нет у .exe."""
    print(message)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('Kraken — сервер', message)
        root.destroy()
    except Exception:                              # noqa: BLE001
        pass


def main():
    cfg = remote.load_settings()
    error = ''
    # Спрашиваем, пока не получим рабочие настройки или пока не закроют окно.
    # Один проход был бы хуже: ошибся в адресе — и запускай программу заново.
    while True:
        if not cfg.get('host') or error:
            cfg = ask_settings(cfg, error)
            if not cfg:
                return 1
            remote.save_settings(cfg)
        process, error = remote.open_tunnel(cfg)
        if process:
            break

    def shutdown():
        # Туннель НЕ демон и переживёт окно, заняв порт до перезагрузки.
        try:
            process.terminate()
        except Exception:                          # noqa: BLE001
            pass

    # Сторож на случай, если соединение оборвётся при открытом окне. Без него
    # панель показывала бы последние удачно загруженные числа как текущие.
    def watch():
        process.wait()
        print('Соединение с сервером разорвано.')

    threading.Thread(target=watch, daemon=True).start()

    try:
        if not open_window(remote.url(), lambda: shutdown()):
            open_in_browser(remote.url())
            input('Окно открыто в браузере. Нажмите Enter, чтобы отключиться…')
    finally:
        shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
