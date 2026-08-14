"""
Обновление СОБРАННОГО приложения (Kraken.exe) через выпуски на GitHub.

ЗАЧЕМ ОТДЕЛЬНО ОТ updater.py. Тот обновляет исходники через git: перематывает
рабочий каталог на свежий коммит и прогоняет тесты. Внутри .exe нет ни git, ни
рабочего каталога, ни pytest — там вообще нет отдельных файлов кода, всё
запаковано в один двоичный файл. Обновлять его можно только одним способом:
скачать новый файл и заменить им себя.

КАК ЭТО РАБОТАЕТ

  Версия зашита в сборку. Файл VERSION попадает внутрь .exe при сборке и
  содержит имя выпуска (например v1.4.0). Сравнивать по дате файла нельзя:
  копирование и распаковка её меняют.

  Что доступно — спрашиваем у GitHub. Репозиторий открытый, поэтому
  обращение анонимное, без токенов: /releases/latest отдаёт имя последнего
  выпуска и ссылку на приложенный Kraken.exe.

  Замена себя — через отдельный .bat. Работающий .exe нельзя перезаписать:
  Windows держит файл занятым. Поэтому новая версия скачивается рядом, а
  подменяет её крошечный сценарий, который ждёт закрытия приложения, меняет
  файлы местами и запускает новое. Старый .exe сохраняется рядом — это и есть
  откат.

ЧЕГО ЗДЕСЬ НЕТ И ПОЧЕМУ. Автоматического отката по тестам, как в git-версии.
Прогнать тесты внутри собранного приложения нечем. Вместо этого прежний файл
не удаляется: если новая версия не запустится, откат возвращает старую одним
нажатием.

ДАННЫЕ НЕ ТРОГАЮТСЯ ВООБЩЕ. Журнал сделок, состояние позиций, настройки и
.env лежат отдельными файлами рядом с приложением. Заменяется ровно один
файл — сам .exe.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

import config
from logger import log

REPO = 'dezet36/kraken-trade-bot'
API_LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
ASSET_NAME = 'Kraken.exe'
NET_TIMEOUT = 30

STATE_FILE = os.path.join(config.DATA_DIR, 'update_state.json')


def is_frozen():
    """Приложение собрано в .exe, а не запущено из исходников."""
    return bool(getattr(sys, 'frozen', False))


def _exe_path():
    return os.path.abspath(sys.executable)


def current_version():
    """
    Версия из файла VERSION внутри сборки.

    Отсутствие файла — это сборка «из рук», собранная мимо выпуска. Такую
    обновлять нельзя: сравнивать не с чем, и любое сравнение соврёт.
    """
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, 'VERSION')
    try:
        # utf-8-sig, а не utf-8: почти любой редактор и PowerShell на Windows
        # пишут файл с меткой порядка байт, и обычный utf-8 отдаёт её первым
        # символом строки. Видно её не было, а сравнение версий строгое —
        # 'v1.0.9' != '﻿v1.0.9', — и приложение вечно предлагало бы
        # обновиться на ту же самую версию, которая уже установлена.
        with open(path, encoding='utf-8-sig') as fh:
            return fh.read().strip().lstrip('﻿')
    except OSError:
        return ''


def _fetch_latest():
    """Последний выпуск с GitHub. Репозиторий открытый — без токенов."""
    request = urllib.request.Request(
        API_LATEST,
        headers={'Accept': 'application/vnd.github+json',
                 'User-Agent': 'kraken-bot-updater'})
    with urllib.request.urlopen(request, timeout=NET_TIMEOUT) as response:
        return json.loads(response.read().decode('utf-8'))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Перенаправление не выполняем: нам нужен адрес, а не страница."""

    def redirect_request(self, *args, **kwargs):
        return None


def _fetch_latest_without_api():
    """
    Версия выпуска БЕЗ обращения к API. Запасной путь, и он важнее основного.

    ЗАЧЕМ ОН НУЖЕН. У API GitHub предел в шестьдесят запросов в час на адрес,
    и считается он без разбора: на сервере с общим или облачным адресом его
    исчерпывает кто угодно, а бот получает 403 и говорит «не удалось связаться
    с GitHub». Выглядит как поломка сети, хотя сеть в порядке и файл выпуска
    лежит на месте.

    Обычная страница `/releases/latest` отвечает перенаправлением на страницу
    последней метки. Это не API, предел на неё не распространяется, и версия
    читается прямо из адреса. Имя файла у нас всегда одно, поэтому и ссылку на
    скачивание можно собрать самим.

    Чего этот путь НЕ даёт — описания выпуска. Поэтому он именно запасной:
    предлагать обновление без объяснения, что в нём, значит просить доверять
    вслепую. Но обновиться без описания лучше, чем не обновиться вовсе.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        f'https://github.com/{REPO}/releases/latest',
        headers={'User-Agent': 'kraken-bot-updater'})
    location = None
    try:
        with opener.open(request, timeout=NET_TIMEOUT) as response:
            location = response.headers.get('Location')
    except urllib.error.HTTPError as exc:
        # Перенаправление приходит именно так: обработчик выше отказался его
        # выполнять, и urllib поднимает исключение с нужными заголовками.
        location = exc.headers.get('Location') if exc.headers else None
        if exc.code not in (301, 302, 303, 307, 308):
            raise
    if not location:
        raise RuntimeError('GitHub не назвал последнюю версию')
    tag = location.rstrip('/').rsplit('/', 1)[-1].strip()
    if not tag:
        raise RuntimeError('в ответе GitHub нет имени версии')
    return {
        'tag_name': tag,
        'name': tag,
        'published_at': '',
        'body': '',
        'assets': [{'name': ASSET_NAME, 'size': 0,
                    'browser_download_url':
                        f'https://github.com/{REPO}/releases/download/'
                        f'{tag}/{ASSET_NAME}'}],
        'from_fallback': True,
    }


def _explain(exc):
    """Человеческое объяснение отказа вместо номера ошибки."""
    code = getattr(exc, 'code', None)
    if code == 403:
        return ('GitHub временно отказывает: у него предел в 60 обращений '
                'в час на один адрес, и он исчерпан. Через час пройдёт само')
    if code == 404:
        return 'выпуск не найден на GitHub'
    return str(exc)


def _asset_url(release):
    for asset in release.get('assets') or []:
        if asset.get('name') == ASSET_NAME:
            return asset.get('browser_download_url'), int(asset.get('size') or 0)
    return None, 0


def _load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(data):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"не удалось сохранить состояние обновления: {exc}")


def status(fetch=True):
    """
    Что установлено, что доступно, можно ли обновляться.

    Форма ответа намеренно совпадает с git-версией: панель обновления на
    дашборде одна, и знать, как именно устроено приложение, ей незачем.
    """
    version = current_version()
    current = {'commit': version or 'без версии', 'date': '', 'subject':
               'собранное приложение'}

    if not version:
        return {'available': False, 'can_update': False, 'current': current,
                'reason': 'сборка без версии — обновлять нечего и не с чем сравнивать'}

    if not fetch:
        state = _load_state()
        return {'available': True, 'mode': 'exe', 'branch': 'выпуски',
                'current': current, 'behind': 0, 'ahead': 0, 'pending': [],
                'dirty': [], 'can_update': False, 'reason': '',
                'previous': state.get('previous')}

    # СНАЧАЛА API, ПОТОМ ОБЫЧНАЯ СТРАНИЦА. У API есть описание выпуска, но и
    # предел в шестьдесят обращений в час на адрес — на сервере с общим или
    # облачным адресом его исчерпывает кто угодно, и бот отвечал «не удалось
    # связаться с GitHub» при исправной сети и лежащем на месте файле.
    #
    # Страница выпусков предела не имеет и версию называет перенаправлением.
    # Описания она не даёт, поэтому идёт второй: обновляться без объяснения,
    # что внутри, хуже, чем с ним, — но лучше, чем не обновляться вовсе.
    release, first_error = None, None
    try:
        release = _fetch_latest()
    except Exception as exc:                       # noqa: BLE001
        first_error = exc
        try:
            release = _fetch_latest_without_api()
        except Exception:                          # noqa: BLE001
            release = None

    if release is None:
        return {'available': True, 'mode': 'exe', 'branch': 'выпуски',
                'current': current, 'behind': 0, 'ahead': 0, 'pending': [],
                'dirty': [], 'can_update': False,
                'reason': f'не удалось связаться с GitHub: {_explain(first_error)}'}

    tag = (release.get('tag_name') or '').strip()
    url, size = _asset_url(release)

    pending, behind, reason = [], 0, ''
    if not tag:
        reason = 'у последнего выпуска нет имени версии'
    elif tag == version:
        reason = 'установлена последняя версия'
    elif not url:
        reason = f'в выпуске {tag} нет файла {ASSET_NAME}'
    else:
        behind = 1
        # Описание выпуска показывается на панели обновления: без него
        # предложение «обновиться» ничем не обосновано, и нажимать его —
        # доверять вслепую.
        notes = (release.get('body') or '').strip()
        pending = [{'commit': tag,
                    'date': (release.get('published_at') or '')[:10],
                    'subject': (release.get('name') or tag),
                    'notes': notes[:2000]}]

    return {
        'available': True, 'mode': 'exe', 'branch': 'выпуски',
        'current': current, 'behind': behind, 'ahead': 0,
        'pending': pending, 'dirty': [],
        'can_update': bool(behind and url),
        'reason': reason,
        'previous': _load_state().get('previous'),
        'download': url, 'size': size, 'tag': tag,
    }


def _download(url, target, expected_size=0):
    request = urllib.request.Request(
        url, headers={'User-Agent': 'kraken-bot-updater',
                      'Accept': 'application/octet-stream'})
    with urllib.request.urlopen(request, timeout=NET_TIMEOUT) as response:
        with open(target, 'wb') as fh:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
    got = os.path.getsize(target)
    # Оборванная закачка даёт файл меньше объявленного. Подменять себя
    # обрубком — верный способ получить приложение, которое не запускается.
    if expected_size and got != expected_size:
        os.remove(target)
        raise IOError(f'скачано {got} байт вместо {expected_size}')
    if got < 1_000_000:
        os.remove(target)
        raise IOError(f'файл подозрительно мал: {got} байт')


def _swap_script(exe, new_exe, old_exe):
    """
    Сценарий подмены. Работающий .exe перезаписать нельзя — Windows держит
    файл занятым, — поэтому меняет файлы местами тот, кто переживёт наше
    закрытие.
    """
    # Комментарии внутри .bat латиницей: cmd.exe читает файл в кодировке
    # консоли, и кириллица в нём превращается в мусор на части систем.
    script = os.path.join(config.DATA_DIR, 'apply_update.bat')
    name = os.path.basename(exe)
    # Сценарий ведёт свой журнал: он работает уже после закрытия приложения,
    # и без следа разобраться, дошёл ли он до подмены, было бы нечем.
    log_file = os.path.join(config.DATA_DIR, 'apply_update.log')
    body = f"""@echo off
echo [%date% %time%] swap started >> "{log_file}"
rem Wait until the app closes and releases its own file.
rem Pause is done with ping, not timeout: timeout reads the console input
rem handle and dies with "input redirection is not supported" when it does not
rem have one - which is exactly the case for a background process.
set TRIES=0
:wait
ping -n 2 127.0.0.1 >nul
tasklist /fi "IMAGENAME eq {name}" | find /i "{name}" >nul
if errorlevel 1 goto swap
set /a TRIES+=1
if %TRIES% LSS 45 goto wait

rem The app is still running after the wait: its file is locked and any move
rem would fail. Touch nothing - the downloaded file stays for the next try.
goto stuck

:swap
if exist "{old_exe}" del /f /q "{old_exe}"
move /y "{exe}" "{old_exe}" >nul
if errorlevel 1 goto stuck
move /y "{new_exe}" "{exe}" >nul
if errorlevel 1 (
    rem Second move failed - put the previous app back so the user is not
    rem left without a program at all.
    move /y "{old_exe}" "{exe}" >nul
)
echo [%date% %time%] swapped, starting app >> "{log_file}"
rem --after-update: the new app may find the dashboard port still held by the
rem previous copy - most often a source run (pythonw desktop.py), which our
rem single-instance lock does not see at all. With this flag the app closes
rem that copy without asking: the user pressed "Update" and waits for the new
rem version, not for a dialog. Without it startup stopped right here.
start "" "{exe}" --after-update
exit /b 0

:stuck
echo [%date% %time%] app still running, nothing changed >> "{log_file}"
exit /b 1
"""
    with open(script, 'w', encoding='ascii', errors='replace') as fh:
        fh.write(body)
    return script


def apply():
    """
    Скачивает новую версию и запускает подмену. Приложение после этого
    ЗАКРЫВАЕТСЯ: заменить свой файл, продолжая работать, нельзя.
    """
    info = status(fetch=True)
    if not info.get('can_update'):
        return False, info.get('reason') or 'обновление недоступно', info

    exe = _exe_path()
    new_exe = exe + '.new'
    old_exe = exe + '.old'

    try:
        log(f"Скачиваю {info['tag']}...")
        _download(info['download'], new_exe, info.get('size') or 0)
    except Exception as exc:                       # noqa: BLE001
        return False, f'не удалось скачать обновление: {exc}', info

    _save_state({'previous': {'commit': current_version(),
                              'subject': 'предыдущая версия'},
                 'tag': info['tag']})

    _launch_swap(exe, new_exe, old_exe)
    log(f"Обновление {info['tag']} скачано, приложение закрывается для подмены")
    _close_app()
    info['restart_required'] = True
    return True, f"обновление {info['tag']} установлено, приложение перезапустится", info


def _launch_swap(exe, new_exe, old_exe):
    script = _swap_script(exe, new_exe, old_exe)
    # Потоки ввода-вывода ОБЯЗАТЕЛЬНО в пустоту.
    #
    # Приложение собрано с --windowed, консоли у него нет, и стандартные
    # потоки равны None. Popen по умолчанию передаёт их дочернему процессу —
    # тот получает недействительные дескрипторы и умирает сразу, не сказав ни
    # слова. Popen при этом отрабатывает без ошибки, и обновление выглядит
    # запущенным. Проверено дважды: сценарий, запущенный руками, подменял файл
    # безупречно, а тот же сценарий из приложения не выполнялся вовсе.
    #
    # CREATE_NO_WINDOW, а не DETACHED_PROCESS. Отсоединённый процесс остаётся
    # вообще без консоли, а сценарию она нужна: tasklist и find без неё не
    # работают. Проверено — сценарий запускался, писал первую строку в журнал и
    # обрывался в цикле ожидания. CREATE_NO_WINDOW даёт консоль, но невидимую.
    # Закрытие приложения сценарий переживёт и так: Windows не убивает дочерние
    # процессы вслед за родителем.
    subprocess.Popen(['cmd', '/c', script],
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     cwd=config.DATA_DIR,
                     env=_clean_env(),
                     close_fds=True)


def _clean_env():
    """
    Окружение без служебных переменных упаковщика.

    Собранное приложение распаковывает себя во временную папку и сообщает её
    путь дочерним процессам переменной _MEIPASS2. Сценарий подмены передаёт
    окружение дальше — новому экземпляру приложения. Тот видит переменную,
    решает, что распаковка уже сделана, и идёт грузить python313.dll из папки,
    которую прежний экземпляр к тому моменту удалил.

    Наблюдалось ровно так: файл подменялся правильно, приложение
    перезапускалось и показывало «Failed to load Python DLL». Причём сам файл
    был исправен — запущенный отдельно, он работал.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('_MEI') and not k.startswith('_PYI')}
    return env


def _close_app():
    """
    Закрывает приложение, чтобы оно отпустило свой файл.

    Без этого шага обновление выглядело выполненным и не выполнялось: новая
    версия скачивалась, сценарий подмены запускался, ждал полминуты, упирался
    в занятый файл и уходил ни с чем. Единственный видимый след — лишний
    файл .new рядом.

    Сначала закрываем окно — оно должно исчезнуть с экрана сразу, иначе
    происходящее выглядит как зависание. Но одного этого мало: закрытие окна
    процесс НЕ завершает. Планировщик держит пул потоков, а интерпретатор при
    выходе их дожидается, и приложение остаётся в памяти вместе с замком на
    своём файле — подмена снова упирается в занятый файл. Проверено дважды на
    собранном приложении: окно закрывалось, файл не менялся.

    Поэтому после закрытия окна выходим принудительно. Состояние от этого не
    теряется: и фантомный счёт, и позиции пишутся на диск в конце каждого
    цикла, а не в момент выхода.

    Задержка перед всем этим нужна, чтобы дашборд успел отдать ответ на
    нажатие: иначе вместо сообщения об успехе человек увидит оборванное
    соединение и решит, что сломалось.
    """
    import threading
    import time

    def bye():
        try:
            import webview
            for window in list(getattr(webview, 'windows', []) or []):
                window.destroy()
        except Exception:                          # noqa: BLE001
            pass
        time.sleep(2)
        os._exit(0)

    threading.Timer(2.0, bye).start()


def rollback():
    """Возвращает предыдущую версию: она лежит рядом как .old."""
    exe = _exe_path()
    old_exe = exe + '.old'
    if not os.path.exists(old_exe):
        return False, 'предыдущей версии рядом нет'

    # Порядок тот же, что при обновлении: текущий уезжает в сторону, на его
    # место встаёт отложенный. Разница только в том, какой из них какой.
    _launch_swap(exe, old_exe, exe + '.rolled')
    _close_app()
    return True, 'возвращаю предыдущую версию, приложение перезапустится'
