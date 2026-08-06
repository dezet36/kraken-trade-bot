"""
Первый запуск приложения: окно с вопросами вместо правки .env руками.

ЗАЧЕМ. Без ключей биржи бот не стартует вообще — падает на первом обращении к
бирже, и человек видит только окно «дашборд не поднялся». Прежний путь был
такой: закрыть приложение, найти рядом файл .env, открыть его блокнотом,
вписать ключи, запустить снова. Это четыре шага, на каждом из которых
ошибаются: правят не тот файл, оставляют кавычки вокруг ключа, добавляют
пробел в конце — а биржа на пробел отвечает «неверная подпись», и причину
потом ищут долго.

ЧТО ЗДЕСЬ ЕСТЬ, ЧЕГО НЕ БЫЛО В ФАЙЛЕ. Проверка. Введённые ключи сразу
пробуются на бирже — тем самым способом и на том же адресе, каким потом будет
ходить бот. Не подошли — окно остаётся открытым и говорит, что ответила биржа.
Ошибиться и узнать об этом через час торговли нельзя.

ПОЧЕМУ TKINTER, А НЕ СТРАНИЦА В ОКНЕ ПРИЛОЖЕНИЯ. Страницу рисует дашборд, а
дашборд поднимает бот, который без ключей не запускается. Курица и яйцо.
Tkinter входит в стандартную библиотеку и работает до всего остального.
"""

import os
import sys

import config
from logger import log

EXCHANGES = ('bybit', 'bingx')
MODES = ('PAPER', 'DEMO', 'LIVE')

ENV_PATH = os.path.join(config.DATA_DIR, '.env')


def _keys_for(exchange):
    prefix = exchange.upper()
    return (os.getenv(f'{prefix}_API_KEY') or '').strip(), \
           (os.getenv(f'{prefix}_SECRET_KEY') or '').strip()


def needs_setup():
    """Ключей нет — спрашивать обязательно, без них бот не поднимется."""
    key, secret = _keys_for(config.EXCHANGE_NAME)
    return not (key and secret)


def _write_env(values):
    """
    Дописывает значения в .env, сохраняя всё остальное.

    Значение подставляется в СУЩЕСТВУЮЩУЮ строку, а не дописывается в конец:
    иначе в файле оказались бы два TRADING_MODE, и какой из них подействует —
    вопрос порядка чтения, а не намерения.
    """
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding='utf-8') as fh:
            lines = fh.read().splitlines()

    for key, value in values.items():
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f'{key}=') or stripped.startswith(f'{key} ='):
                lines[i] = f'{key}={value}'
                replaced = True
        if not replaced:
            lines.append(f'{key}={value}')

    with open(ENV_PATH, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines).rstrip() + '\n')


def check_keys(exchange, mode, key, secret):
    """
    Пробует ключи на бирже так же, как это потом сделает бот.

    Адрес выбирается по режиму: у демо-счёта он свой, и демо-ключи на боевом
    адресе не работают. Проверка «просто ключи валидные» без учёта режима
    пропустила бы самую частую ошибку — демо-ключи при TRADING_MODE=LIVE.
    """
    import exchange as ex

    endpoint = 'DEMO' if mode in ('DEMO', 'PAPER') else 'LIVE'
    try:
        client = ex.make_client(exchange, key, secret, endpoint)
        client.fetch_balance()
        return True, ''
    except Exception as exc:                       # noqa: BLE001
        return False, str(exc)[:300]


def confirm_live():
    """
    Подтверждение боевого режима для окна: то же требование, что в консоли.

    В консоли LIVE подтверждается двукратным вводом слова YES. Смысл не в
    словах, а в том, что случайным двойным щелчком реальные деньги торговаться
    не начнут. У окна клавиатурного ввода нет, поэтому здесь его аналог —
    набрать то же слово руками. Кнопка «да» этому требованию не отвечает: по
    ней промахиваются.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        return False

    answer = {'ok': False}
    root = tk.Tk()
    root.title('Kraken — РЕАЛЬНЫЕ ДЕНЬГИ')
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()

    ttk.Label(frame, text='Запуск на реальные деньги',
              font=('Segoe UI', 12, 'bold'), foreground='#a00'
              ).grid(row=0, column=0, columnspan=2, sticky='w')
    ttk.Label(frame, wraplength=420, foreground='#555',
              text=(f'Биржа {config.EXCHANGE_NAME.upper()}, риск на сделку '
                    f'{config.RISK_PER_TRADE}%, пул {len(config.TRADING_PAIRS_POOL)} пар.\n'
                    'Бот будет открывать позиции настоящими деньгами.')
              ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 12))
    ttk.Label(frame, text='Введите YES:').grid(row=2, column=0, sticky='w')
    value = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=value, width=20)
    entry.grid(row=2, column=1, sticky='w')
    entry.focus_set()
    status = ttk.Label(frame, text='', foreground='#a00')
    status.grid(row=3, column=0, columnspan=2, sticky='w', pady=(6, 0))

    def go():
        if value.get().strip() != 'YES':
            status.config(text='Нужно ввести ровно YES заглавными.')
            return
        answer['ok'] = True
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=4, column=0, columnspan=2, sticky='e', pady=(14, 0))
    ttk.Button(buttons, text='Запустить', command=go).pack(side='right')
    ttk.Button(buttons, text='Отмена',
               command=root.destroy).pack(side='right', padx=(0, 8))
    root.bind('<Return>', lambda _e: go())

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f'+{max(x, 0)}+{max(y, 0)}')
    root.mainloop()
    return answer['ok']


def run_setup():
    """
    Показывает окно настройки. True — настроено, можно запускать бота.

    False возвращается, когда окно закрыли не заполнив: запускать бота в этом
    случае бессмысленно, он всё равно упадёт на первом обращении к бирже.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        log('tkinter недоступен — окно настройки показать нечем')
        return False

    result = {'ok': False}

    root = tk.Tk()
    root.title('Kraken — первая настройка')
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.grid()

    ttk.Label(frame, text='Ключи биржи',
              font=('Segoe UI', 12, 'bold')).grid(row=0, column=0, columnspan=2,
                                                  sticky='w')
    ttk.Label(frame, wraplength=430, foreground='#555',
              text='Нужны даже для фантомной торговли: котировки берутся с биржи. '
                   'Для режимов «фантом» и «демо» подойдут ключи демо-счёта.'
              ).grid(row=1, column=0, columnspan=2, sticky='w', pady=(2, 12))

    exchange_var = tk.StringVar(value=config.EXCHANGE_NAME
                                if config.EXCHANGE_NAME in EXCHANGES else 'bybit')
    # На первом запуске .env ещё нет, и config отдаёт своё значение по
    # умолчанию — DEMO. Предлагать новичку сразу отправлять ордера на биржу
    # неправильно: начинать надо с фантома. Значение из файла уважаем, если он
    # есть: человек его туда вписал осознанно.
    from_file = (os.path.exists(ENV_PATH)
                 and 'TRADING_MODE' in open(ENV_PATH, encoding='utf-8').read())
    mode_var = tk.StringVar(value=config.TRADING_MODE
                            if from_file and config.TRADING_MODE in MODES
                            else 'PAPER')
    key_var = tk.StringVar()
    secret_var = tk.StringVar()

    ttk.Label(frame, text='Биржа').grid(row=2, column=0, sticky='w')
    box = ttk.Frame(frame); box.grid(row=2, column=1, sticky='w')
    for name in EXCHANGES:
        ttk.Radiobutton(box, text=name, value=name,
                        variable=exchange_var).pack(side='left', padx=(0, 10))

    ttk.Label(frame, text='Режим').grid(row=3, column=0, sticky='w', pady=(8, 0))
    box2 = ttk.Frame(frame); box2.grid(row=3, column=1, sticky='w', pady=(8, 0))
    labels = {'PAPER': 'фантом', 'DEMO': 'демо-счёт', 'LIVE': 'реальные деньги'}
    for name in MODES:
        ttk.Radiobutton(box2, text=labels[name], value=name,
                        variable=mode_var).pack(side='left', padx=(0, 10))

    ttk.Label(frame, text='API key').grid(row=4, column=0, sticky='w', pady=(12, 0))
    ttk.Entry(frame, textvariable=key_var, width=46).grid(row=4, column=1,
                                                          pady=(12, 0))
    ttk.Label(frame, text='Secret key').grid(row=5, column=0, sticky='w', pady=(6, 0))
    ttk.Entry(frame, textvariable=secret_var, width=46,
              show='•').grid(row=5, column=1, pady=(6, 0))

    status = ttk.Label(frame, text='', wraplength=430, foreground='#a00')
    status.grid(row=6, column=0, columnspan=2, sticky='w', pady=(10, 0))

    buttons = ttk.Frame(frame)
    buttons.grid(row=7, column=0, columnspan=2, sticky='e', pady=(14, 0))
    save_btn = ttk.Button(buttons, text='Проверить и сохранить')
    save_btn.pack(side='right')
    ttk.Button(buttons, text='Выход',
               command=root.destroy).pack(side='right', padx=(0, 8))

    def submit():
        # Пробелы по краям срезаем молча: ключ, скопированный из браузера,
        # часто приезжает с пробелом на конце, биржа отвечает «неверная
        # подпись», и связать одно с другим потом трудно.
        key = key_var.get().strip()
        secret = secret_var.get().strip()
        if not key or not secret:
            status.config(text='Заполните оба поля.', foreground='#a00')
            return

        exchange, mode = exchange_var.get(), mode_var.get()
        status.config(text='Проверяю на бирже…', foreground='#555')
        save_btn.config(state='disabled')
        root.update()

        ok, error = check_keys(exchange, mode, key, secret)
        save_btn.config(state='normal')
        if not ok:
            status.config(text=f'Биржа не приняла ключи: {error}', foreground='#a00')
            return

        prefix = exchange.upper()
        values = {'EXCHANGE': exchange, 'TRADING_MODE': mode,
                  f'{prefix}_API_KEY': key, f'{prefix}_SECRET_KEY': secret}
        if mode == 'LIVE':
            # У окна нет консоли, а LIVE требует подтверждения с клавиатуры.
            # Выбор режима здесь и есть осознанное подтверждение.
            values['LIVE_CONFIRMED'] = 'YES'
        _write_env(values)

        # Значения нужны прямо сейчас, в этом же процессе: бот запускается
        # следом и читает их из окружения, а не перечитывает файл.
        for name, value in values.items():
            os.environ[name] = value
        import importlib
        importlib.reload(config)

        result['ok'] = True
        root.destroy()

    save_btn.config(command=submit)
    root.bind('<Return>', lambda _event: submit())

    # По центру экрана: окно маленькое, в углу его можно не заметить.
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f'+{max(x, 0)}+{max(y, 0)}')

    root.mainloop()
    return result['ok']
