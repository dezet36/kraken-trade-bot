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
        with open(path, encoding='utf-8') as fh:
            return fh.read().strip()
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

    try:
        release = _fetch_latest()
    except Exception as exc:                       # noqa: BLE001
        return {'available': True, 'mode': 'exe', 'branch': 'выпуски',
                'current': current, 'behind': 0, 'ahead': 0, 'pending': [],
                'dirty': [], 'can_update': False,
                'reason': f'не удалось связаться с GitHub: {exc}'}

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
        pending = [{'commit': tag,
                    'date': (release.get('published_at') or '')[:10],
                    'subject': (release.get('name') or tag)}]

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
    body = f"""@echo off
rem Wait until the app closes and releases its own file.
set TRIES=0
:wait
timeout /t 1 /nobreak >nul
tasklist /fi "IMAGENAME eq {name}" | find /i "{name}" >nul
if errorlevel 1 goto swap
set /a TRIES+=1
if %TRIES% LSS 90 goto wait

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
start "" "{exe}"
exit /b 0

:stuck
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
    # DETACHED_PROCESS: сценарий обязан пережить закрытие приложения, иначе
    # умрёт вместе с ним, не успев ничего подменить. С CREATE_NO_WINDOW его
    # сочетать нельзя — CreateProcess такую пару отвергает.
    subprocess.Popen(['cmd', '/c', script],
                     creationflags=getattr(subprocess, 'DETACHED_PROCESS', 0),
                     close_fds=True)


def _close_app():
    """
    Закрывает приложение, чтобы оно отпустило свой файл.

    Без этого шага обновление выглядело выполненным и не выполнялось: новая
    версия скачивалась, сценарий подмены запускался, ждал полминуты, упирался
    в занятый файл и уходил ни с чем. Единственный видимый след — лишний
    файл .new рядом.

    Сначала пробуем закрыть окно: тогда приложение завершится своим обычным
    путём, дописав состояние на диск. Если окна нет (запуск службой или из
    консоли), выходим принудительно — но с задержкой, чтобы дашборд успел
    отдать ответ на нажатие, иначе человек увидит оборванное соединение
    вместо сообщения об успехе.
    """
    import threading

    def bye():
        try:
            import webview
            windows = list(getattr(webview, 'windows', []) or [])
            if windows:
                for window in windows:
                    window.destroy()
                return
        except Exception:                          # noqa: BLE001
            pass
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
