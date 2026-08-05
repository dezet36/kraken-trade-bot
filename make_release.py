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

Запуск:
    python make_release.py            -> release/
    python make_release.py --zip      -> release/ + kraken-bot.zip
"""

import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, 'release')

# Код и всё, без чего бот не запустится.
INCLUDE_DIRS = ('Live_Bot',)
INCLUDE_FILES = (
    'requirements.txt', '.env.example',
    'install.sh', 'run.sh', 'install_service.sh',
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
    'Live_Bot/exchange.py', 'Live_Bot/strategy.py', 'Live_Bot/strategy_smc.py',
    'Live_Bot/paper_broker.py', 'Live_Bot/smc/params.py',
    'requirements.txt', '.env.example', 'install.sh', 'install.ps1',
)

# Чего в сборке быть не должно. Проверяется рекурсивно.
MUST_NOT_EXIST = ('.env', 'platform.db', 'trades_journal.csv',
                  'positions_state.json', 'paper_trades.csv', 'bot_log.txt')


def _ignore(directory, names):
    skip = []
    for name in names:
        if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIXES):
            skip.append(name)
    return skip


def build(make_zip=False):
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    copied = 0
    for name in INCLUDE_DIRS:
        src = os.path.join(ROOT, name)
        if not os.path.isdir(src):
            print(f'   пропущено (нет каталога): {name}')
            continue
        shutil.copytree(src, os.path.join(OUT, name), ignore=_ignore)
        copied += 1

    for name in INCLUDE_FILES:
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
    sys.exit(build(make_zip='--zip' in sys.argv))
