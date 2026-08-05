"""
Сборка папки для сервера: скопировал, запустил, работает.

Что делает: складывает в release/ только то, что нужно боту в работе, и
проверяет, что внутрь не попало лишнего. Исследования, кэши свечей на сотни
мегабайт, история чужих сделок и — главное — ключи остаются здесь.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ СКРИПТ, А НЕ «СКОПИРУЙ ПАПКУ РУКАМИ». Ручное
копирование даёт две ошибки, обе дорогие. Первая: в архив уезжает .env с
боевыми ключами, и они оказываются там, где им быть не следует. Вторая:
уезжает старый positions_state.json, и свежепоставленный бот на сервере
считает, что у него уже открыты позиции, которых на бирже нет. Здесь оба
случая исключены списком и проверкой после сборки.

ДВА ВИДА СБОРКИ

    --git   (по умолчанию, если git доступен) — release/ становится
            частичным клоном репозитория с разреженной выкладкой: внутри
            только рабочие файлы, но это НАСТОЯЩИЙ git-репозиторий. Кнопка
            «Обновить» на дашборде работает; на сервере ничего скачивать
            руками не нужно.

    --copy  простое копирование. Папка без git: обновлять придётся
            копированием новой версии поверх старой (update.sh). Нужен,
            когда на сервере нет git или доступа к GitHub.

Запуск:
    python make_release.py            -> release/ (git, если возможно)
    python make_release.py --copy     -> release/ без git
    python make_release.py --zip      -> + kraken-bot.zip
"""

import os
import shutil
import stat
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, 'release')

# Код и всё, без чего бот не запустится.
INCLUDE_DIRS = ('Live_Bot',)
INCLUDE_FILES = (
    'requirements.txt', '.env.example',
    'install.sh', 'run.sh', 'install_service.sh', 'update.sh',
    'install.ps1', 'run.ps1',
    'Dockerfile', 'docker-compose.yml', 'start_server.sh', 'stop_server.sh',
    'README_СЕРВЕР.md', 'DEPLOY.md',
)

# Не копировать НИКОГДА. Первые три пункта — причина существования скрипта.
EXCLUDE_NAMES = {
    '.env',                      # ключи биржи
    'platform.db',               # пользователи и их ключи
    'trades_journal.csv', 'trades_detail.jsonl', 'trades.csv',
    'paper_trades.csv', 'paper_trades.jsonl', 'paper_state.json',
    'positions_state.json',      # «открытые позиции», которых на сервере нет
    'pending_orders.json', 'cooldown_state.json', 'pair_strategy.json',
    'runtime_settings.json', 'update_state.json',
    'bot_log.txt', '__pycache__', 'app_window', '.pytest_cache',
}
EXCLUDE_SUFFIXES = ('.pyc', '.pyo', '.log', '.bak', '.db', '.db-wal', '.db-shm')

# То, что обязано оказаться в сборке. Проверяется после копирования: список
# файлов легко разъезжается с реальностью при переименованиях.
MUST_EXIST = (
    'Live_Bot/bot.py', 'Live_Bot/config.py', 'Live_Bot/dashboard.py',
    'Live_Bot/dashboard.html', 'Live_Bot/doctor.py', 'Live_Bot/updater.py',
    'Live_Bot/exchange.py', 'Live_Bot/paper_broker.py',
    # Три стратегии и их ядра: пропажа любого файла ломает бота на сервере
    # молча — стратегия просто «не находит сетапов».
    'Live_Bot/strategy.py',
    'Live_Bot/strategy_smc.py', 'Live_Bot/smc/params.py', 'Live_Bot/smc/signal.py',
    'Live_Bot/strategy_levels.py', 'Live_Bot/levels/core.py',
    'Live_Bot/levels/params.py',
    # Общая инфраструктура
    'Live_Bot/market_regime.py', 'Live_Bot/error_log.py',
    'Live_Bot/settings_store.py', 'Live_Bot/exit_plan.py',
    'requirements.txt', '.env.example', 'install.sh', 'install.ps1',
    'update.sh',
)

# Чего в сборке быть не должно. Проверяется рекурсивно.
MUST_NOT_EXIST = ('.env', 'platform.db', 'trades_journal.csv',
                  'positions_state.json', 'paper_trades.csv', 'bot_log.txt')


def _force_remove(func, path, _exc):
    """
    Снимает защиту от записи и повторяет удаление.

    Git помечает файлы в .git/objects только для чтения, и обычный rmtree на
    них падает. Без этого ВТОРАЯ сборка подряд всегда завершалась ошибкой —
    первая проходила, потому что удалять было нечего.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:                              # noqa: BLE001
        pass


def _rmtree(path):
    shutil.rmtree(path, onexc=_force_remove)


def _ignore(directory, names):
    skip = []
    for name in names:
        if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIXES):
            skip.append(name)
    return skip


def remote_url():
    try:
        proc = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)
        return proc.stdout.strip() if proc.returncode == 0 else ''
    except Exception:                              # noqa: BLE001
        return ''


def current_branch():
    try:
        proc = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)
        return proc.stdout.strip() if proc.returncode == 0 else 'main'
    except Exception:                              # noqa: BLE001
        return 'main'


def build_git():
    """
    Сборка разреженным клоном: папка содержит только рабочие файлы, но
    остаётся полноценным git-репозиторием.

    Именно это позволяет кнопке «Обновить» на дашборде работать на сервере:
    обычная копия папки git-репозиторием не является, и обновляться ей
    нечем. --filter=blob:none не тянет историю файлов, поэтому клон весит
    столько же, сколько копия.
    """
    url, branch = remote_url(), current_branch()
    if not url:
        return False, 'у репозитория нет origin'

    paths = list(INCLUDE_DIRS) + [f for f in INCLUDE_FILES]
    cmds = [
        ['git', 'clone', '--filter=blob:none', '--no-checkout',
         '--branch', branch, url, OUT],
        ['git', '-C', OUT, 'sparse-checkout', 'init', '--no-cone'],
        ['git', '-C', OUT, 'sparse-checkout', 'set'] + paths,
        ['git', '-C', OUT, 'checkout', branch],
    ]
    for cmd in cmds:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=300)
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout).strip()[:200]
    return True, f'{url} ({branch})'


def build(make_zip=False, use_git=True):
    if os.path.exists(OUT):
        _rmtree(OUT)

    git_ok, git_note = (False, 'отключено ключом --copy')
    if use_git:
        git_ok, git_note = build_git()
        if git_ok:
            print(f'   разреженный клон: {git_note}')
        else:
            print(f'   клон не удался ({git_note}) — обычное копирование')
            if os.path.exists(OUT):
                _rmtree(OUT)

    if not os.path.exists(OUT):
        os.makedirs(OUT)

    copied = 0
    if git_ok:
        # Файлы уже на месте из клона; докладывать нечего, но проверки и
        # каталог данных нужны те же.
        copied = len(INCLUDE_DIRS) + len(INCLUDE_FILES)
    for name in (() if git_ok else INCLUDE_DIRS):
        src = os.path.join(ROOT, name)
        if not os.path.isdir(src):
            print(f'   пропущено (нет каталога): {name}')
            continue
        shutil.copytree(src, os.path.join(OUT, name), ignore=_ignore)
        copied += 1

    for name in (() if git_ok else INCLUDE_FILES):
        src = os.path.join(ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(OUT, name))
            copied += 1
        else:
            print(f'   пропущено (нет файла): {name}')

    # Каталог данных создаётся пустым: так видно, куда всё ляжет, и
    # установщику не нужно гадать с правами.
    os.makedirs(os.path.join(OUT, 'bot_data'), exist_ok=True)
    with open(os.path.join(OUT, 'bot_data', 'README.txt'), 'w', encoding='utf-8') as fh:
        fh.write('Каталог данных бота.\n\n'
                 'Здесь появятся .env с ключами, журнал сделок, состояние\n'
                 'позиций и настройки. Обновление кода этот каталог не\n'
                 'трогает: папку с кодом можно перезаписывать целиком.\n\n'
                 'Резервная копия бота = резервная копия этого каталога.\n')

    problems = verify()
    print()
    print(f'Собрано в {OUT} ({copied} элементов, {_size(OUT) / 1e6:.1f} МБ)')
    if problems:
        print()
        print('ПРОБЛЕМЫ:')
        for p in problems:
            print(f'   {p}')
        return 1

    if make_zip:
        archive = os.path.join(ROOT, 'kraken-bot.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
            for folder, _, files in os.walk(OUT):
                for file in files:
                    full = os.path.join(folder, file)
                    zf.write(full, os.path.relpath(full, OUT))
        print(f'Архив: {archive} ({os.path.getsize(archive) / 1e6:.1f} МБ)')

    print()
    print('Дальше: скопировать папку release на сервер и запустить')
    print('   Linux:   ./install.sh')
    print('   Windows: .\\install.ps1')
    return 0


def verify():
    """Проверка сборки: всё нужное на месте, ничего лишнего не уехало."""
    problems = []
    for rel in MUST_EXIST:
        if not os.path.exists(os.path.join(OUT, rel.replace('/', os.sep))):
            problems.append(f'НЕ ХВАТАЕТ: {rel}')

    for folder, dirs, files in os.walk(OUT):
        for name in files:
            if name in MUST_NOT_EXIST:
                rel = os.path.relpath(os.path.join(folder, name), OUT)
                problems.append(f'ЛИШНЕЕ (данные или ключи!): {rel}')

    # Синтаксис: битый файл лучше поймать здесь, чем на сервере
    for folder, dirs, files in os.walk(os.path.join(OUT, 'Live_Bot')):
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding='utf-8') as fh:
                    compile(fh.read(), path, 'exec')
            except SyntaxError as exc:
                problems.append(f'НЕ КОМПИЛИРУЕТСЯ: '
                                f'{os.path.relpath(path, OUT)}: {exc}')
    return problems


def _size(path):
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


if __name__ == '__main__':
    sys.exit(build(make_zip='--zip' in sys.argv,
                   use_git='--copy' not in sys.argv))
