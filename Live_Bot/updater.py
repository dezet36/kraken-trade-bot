"""
Обновление кода из git без потери торговых данных.

ЗАЧЕМ. На сервере бот должен уметь подтянуть новую версию стратегий сам, не
дожидаясь ручного захода по ssh. Но кнопка, выполняющая код из интернета, —
самое опасное, что может быть на дашборде, поэтому здесь всё построено
вокруг отказов, а не вокруг удобства.

ЧТО ЗАЩИЩЕНО И ЧЕМ

  Торговые данные не трогаются вообще. Журнал сделок, состояние позиций,
  отложенные ордера, кулдауны, настройки оператора, .env с ключами и
  фантомный счёт не отслеживаются git (см. .gitignore). Обновление
  переносит только отслеживаемые файлы, то есть исключительно код.
  Проверка этого — не предположение, а тест: verify_data_untracked()
  спрашивает у git, и если хоть один файл данных окажется под контролем
  версий, обновление запрещается целиком.

  Только перемотка вперёд. Если на сервере кто-то правил код руками и
  история разошлась, git пришлось бы сливать ветки — в автоматическом
  режиме это способ получить сломанный рабочий каталог. Такое обновление
  отклоняется с объяснением.

  Грязный рабочий каталог блокирует обновление. Незакоммиченные правки
  ОТСЛЕЖИВАЕМЫХ файлов означают, что на сервере меняли код; перезаписать
  их молча нельзя.

  Откат по тестам. После перемотки прогоняется набор тестов. Не прошли —
  код возвращается на прежний коммит автоматически. Бот, который не может
  пройти собственные тесты, не должен торговать деньгами.

  Никакого git clean. Он удалил бы неотслеживаемые файлы, то есть ровно
  все данные.

ЧТО НЕ ДЕЛАЕТСЯ АВТОМАТИЧЕСКИ. Перезапуск. Python держит уже
импортированные модули в памяти, и новый код начнёт действовать только
после рестарта. Обновление сообщает об этом флагом restart_required;
решение принимает оператор или служба-обёртка.
"""

import json
import os
import subprocess
import sys

import config
from logger import log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(config.DATA_DIR, 'update_state.json')
TIMEOUT = 120

# Файлы, которые обязаны оставаться вне контроля версий. Список повторяет
# .gitignore не для красоты: git ls-files по нему выполняется на каждой
# проверке, и расхождение между намерением и фактом ловится сразу.
DATA_FILES = (
    'Live_Bot/.env',
    'Live_Bot/trades_journal.csv',
    'Live_Bot/positions_state.json',
    'Live_Bot/pending_orders.json',
    'Live_Bot/cooldown_state.json',
    'Live_Bot/pair_strategy.json',
    'Live_Bot/runtime_settings.json',
    'Live_Bot/paper_state.json',
    'Live_Bot/paper_trades.csv',
    'Live_Bot/paper_trades.jsonl',
    'Live_Bot/platform.db',
)


def _git(*args, timeout=TIMEOUT):
    """Запускает git в каталоге проекта. Возвращает (код, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ('git',) + args, cwd=ROOT, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, '', 'git не установлен'
    except subprocess.TimeoutExpired:
        return 124, '', f'git не ответил за {timeout} с'
    except Exception as exc:                       # noqa: BLE001
        return 1, '', str(exc)


def is_repo():
    code, out, _ = _git('rev-parse', '--is-inside-work-tree')
    return code == 0 and out == 'true'


def verify_data_untracked():
    """
    Проверяет, что ни один файл с данными не находится под контролем версий.

    Это главный предохранитель. Пока данные неотслеживаемы, git физически не
    может их перезаписать при перемотке. Если же кто-то однажды закоммитит
    журнал сделок, обновление начнёт затирать историю — поэтому проверка
    выполняется перед КАЖДЫМ обновлением, а не один раз при настройке.
    """
    code, out, _ = _git('ls-files', '--', *DATA_FILES)
    if code != 0:
        return False, 'не удалось проверить состав репозитория'
    tracked = [line for line in out.splitlines() if line.strip()]
    if tracked:
        return False, ('под контролем версий оказались файлы данных: '
                       + ', '.join(tracked[:5]))
    return True, ''


def dirty_tracked():
    """Незакоммиченные изменения ОТСЛЕЖИВАЕМЫХ файлов."""
    code, out, _ = _git('status', '--porcelain', '--untracked-files=no')
    if code != 0:
        return []
    return [line[3:] for line in out.splitlines() if line.strip()]


def _commit_info(ref='HEAD'):
    code, out, _ = _git('log', '-1', '--format=%h|%ad|%s', '--date=short', ref)
    if code != 0 or not out:
        return {}
    short, date, subject = (out.split('|', 2) + ['', ''])[:3]
    return {'commit': short, 'date': date, 'subject': subject}


def _app_mode():
    """
    Приложение собрано в .exe — обновляться надо выпусками, а не git.

    Развилка стоит здесь, а не в дашборде: панель обновления одна, и знать,
    как именно устроено то, что она обновляет, ей незачем. Обе реализации
    отвечают словарём одной формы.
    """
    import updater_app
    return updater_app if updater_app.is_frozen() else None


def status(fetch=True):
    """
    Что установлено, что доступно, можно ли обновляться.

    fetch=False — быстрый ответ без обращения к сети (для отрисовки
    страницы, чтобы она не ждала git по несколько секунд).
    """
    app = _app_mode()
    if app is not None:
        return app.status(fetch=fetch)

    if not is_repo():
        return {'available': False, 'can_update': False,
                'reason': 'каталог не является git-репозиторием'}

    code, branch, _ = _git('rev-parse', '--abbrev-ref', 'HEAD')
    branch = branch if code == 0 else '?'
    current = _commit_info()

    fetched, fetch_error = False, ''
    if fetch:
        code, _, err = _git('fetch', '--quiet', 'origin', branch, timeout=60)
        fetched = code == 0
        if not fetched:
            fetch_error = err or 'не удалось связаться с origin'

    upstream = f'origin/{branch}'
    code, out, _ = _git('rev-list', '--count', f'HEAD..{upstream}')
    behind = int(out) if code == 0 and out.isdigit() else 0
    code, out, _ = _git('rev-list', '--count', f'{upstream}..HEAD')
    ahead = int(out) if code == 0 and out.isdigit() else 0

    pending = []
    if behind:
        code, out, _ = _git('log', '--format=%h|%ad|%s', '--date=short',
                            f'HEAD..{upstream}')
        if code == 0:
            for line in out.splitlines()[:20]:
                short, date, subject = (line.split('|', 2) + ['', ''])[:3]
                pending.append({'commit': short, 'date': date, 'subject': subject})

    dirty = dirty_tracked()
    data_ok, data_problem = verify_data_untracked()

    reason = ''
    if fetch and not fetched:
        reason = fetch_error
    elif not data_ok:
        reason = data_problem
    elif dirty:
        reason = f'на сервере есть незакоммиченные правки кода ({len(dirty)})'
    elif ahead:
        reason = (f'локальная история ушла вперёд на {ahead} — перемотка '
                  f'невозможна, нужно разбираться руками')
    elif not behind:
        reason = 'установлена последняя версия'

    return {
        'available': True,
        'branch': branch,
        'current': current,
        'behind': behind,
        'ahead': ahead,
        'pending': pending,
        'dirty': dirty[:10],
        'can_update': bool(behind and not dirty and not ahead and data_ok
                           and (fetched or not fetch)),
        'reason': reason,
        'previous': _load_state().get('previous'),
    }


def _load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:                              # noqa: BLE001
        return {}


def _save_state(data):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:                       # noqa: BLE001
        log(f"⚠️ не удалось сохранить состояние обновления: {exc}")


def run_tests():
    """
    Прогон набора тестов после обновления.

    Смысл не в полноте проверки, а в отсечении явно сломанного кода: бот,
    который не проходит собственные тесты, не должен продолжать торговать.
    """
    tests_dir = os.path.join(ROOT, 'Live_Bot', 'tests')
    if not os.path.isdir(tests_dir):
        return True, 'тестов нет — проверка пропущена'
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'pytest', tests_dir, '-q'],
            cwd=ROOT, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=600)
    except Exception as exc:                       # noqa: BLE001
        return False, f'не удалось запустить тесты: {exc}'
    tail = (proc.stdout or proc.stderr or '').strip().splitlines()
    summary = tail[-1] if tail else ''
    return proc.returncode == 0, summary


def apply():
    """
    Перемотка на свежий код с автоматическим откатом при провале тестов.

    Возвращает (успех, сообщение, подробности).
    """
    app = _app_mode()
    if app is not None:
        return app.apply()

    info = status(fetch=True)
    if not info.get('can_update'):
        return False, info.get('reason') or 'обновление недоступно', info

    previous = info['current'].get('commit')
    branch = info['branch']

    code, out, err = _git('merge', '--ff-only', f'origin/{branch}')
    if code != 0:
        return False, f'перемотка не удалась: {err or out}', info

    _save_state({'previous': previous, 'to': _commit_info().get('commit')})
    log(f"🔄 код обновлён: {previous} -> {_commit_info().get('commit')}")

    ok, summary = run_tests()
    if not ok:
        rolled, message = rollback()
        log(f"❌ тесты после обновления не прошли ({summary}) — {message}")
        return False, (f'тесты не прошли ({summary}). '
                       + ('код возвращён на прежнюю версию' if rolled
                          else f'ОТКАТ НЕ УДАЛСЯ: {message}')), status(fetch=False)

    fresh = status(fetch=False)
    fresh['restart_required'] = True
    fresh['tests'] = summary
    return True, (f'обновлено до {fresh["current"].get("commit")}. {summary}. '
                  f'Изменения вступят в силу после перезапуска.'), fresh


def rollback():
    """Возврат на коммит, который стоял до последнего обновления."""
    app = _app_mode()
    if app is not None:
        return app.rollback()

    previous = _load_state().get('previous')
    if not previous:
        return False, 'нет записи о предыдущей версии'
    code, _, err = _git('reset', '--hard', previous)
    if code != 0:
        return False, f'откат не удался: {err}'
    log(f"↩️ код возвращён на {previous}")
    return True, f'код возвращён на {previous}'
