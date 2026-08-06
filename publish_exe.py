"""
Собрать Kraken.exe и выложить выпуском на GitHub — без GitHub Actions.

ЗАЧЕМ ЭТО ПОЯВИЛОСЬ. Выпуски собирались только в Actions. 2026-08-06 Actions
слёг с крупной аварией на несколько часов: задания либо не стартовали, либо
падали на первом шаге с «Failed to resolve action download info». Обновиться
стало нельзя вовсе, хотя ни код, ни машина сборки были ни при чём.

Это и есть цена единственного пути. Здесь второй: та же сборка и та же
выкладка, но с этой машины. Actions остаётся основным — он собирает на
чистой системе и не зависит от того, что установлено локально, — а этот
скрипт нужен, когда основной путь недоступен.

ЧЕМ АВТОРИЗУЕТСЯ. Тем же доступом, которым git пушит в этот репозиторий: он
лежит в хранилище учётных данных системы и отдаётся по `git credential fill`.
Нового секрета заводить не нужно, и сам токен никуда не печатается — он
живёт в памяти процесса и уходит только в заголовок запроса к GitHub.

ЧТО ДЕЛАЕТ ПО ПОРЯДКУ
    1. записывает VERSION внутрь сборки (без метки порядка байт — с ней
       сравнение версий ломается навсегда, см. updater_app.current_version);
    2. собирает .exe тем же набором ключей, что и Actions;
    3. гоняет самопроверку собранного: все ли модули на месте и узнаёт ли
       сборка себя выпуском;
    4. создаёт выпуск по тегу и прикладывает файл.

Запуск:
    python publish_exe.py v1.0.12
    python publish_exe.py v1.0.12 --no-build     # файл уже собран
    python publish_exe.py v1.0.12 --dry-run      # собрать и проверить, не выкладывая
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(ROOT, 'dist', 'Kraken.exe')
VERSION_FILE = os.path.join(ROOT, 'Live_Bot', 'VERSION')
API = 'https://api.github.com'
ASSET = 'Kraken.exe'


def say(text):
    print(text, flush=True)


def repo_slug():
    """«владелец/имя» из адреса origin — чтобы не держать его вторым списком."""
    url = subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
    slug = url.split('github.com')[-1].lstrip(':/')
    return slug[:-4] if slug.endswith('.git') else slug


def write_version(tag):
    # Строго без метки порядка байт: она невидима в тексте, но сравнение
    # версий строгое, и сборка вечно предлагала бы обновиться сама на себя.
    with open(VERSION_FILE, 'w', encoding='utf-8', newline='') as fh:
        fh.write(tag + '\n')
    say(f'версия внутри сборки: {tag}')


def build():
    if not shutil.which('python'):
        raise SystemExit('python не найден в PATH')
    say('собираю .exe (несколько минут)...')
    args = [
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--onefile', '--windowed', '--name', 'Kraken',
        '--icon', os.path.join(ROOT, 'Live_Bot', 'app_icon.ico'),
        '--distpath', os.path.join(ROOT, 'dist'),
        '--workpath', os.path.join(ROOT, 'build'),
        '--specpath', os.path.join(ROOT, 'build'),
        '--paths', os.path.join(ROOT, 'Live_Bot'),
        '--add-data', os.path.join(ROOT, 'Live_Bot', 'dashboard.html') + ';.',
        '--add-data', os.path.join(ROOT, 'Live_Bot', 'app_icon.ico') + ';.',
        '--add-data', VERSION_FILE + ';.',
        '--add-data', os.path.join(ROOT, '.env.example') + ';.',
        '--collect-all', 'webview',
        '--exclude-module', 'research',
    ]
    for name in ('ccxt.bybit', 'ccxt.bingx',
                 'apscheduler.schedulers.blocking', 'apscheduler.executors.pool',
                 'apscheduler.triggers.interval', 'webview.platforms.winforms',
                 'clr_loader', 'first_run', 'updater_app', 'updater',
                 'tkinter', 'tkinter.ttk'):
        args += ['--hidden-import', name]
    args.append(os.path.join(ROOT, 'Live_Bot', 'desktop.py'))

    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    if result.returncode != 0 or not os.path.exists(EXE):
        raise SystemExit('сборка не удалась:\n' + result.stdout[-2000:]
                         + result.stderr[-2000:])
    say(f'собрано: {os.path.getsize(EXE) / 1048576:.1f} МБ')


def selftest(tag):
    """
    Проверка собранного. Без неё выкладывать нельзя.

    Упаковка ломается молча: потерянный модуль подгружается по имени в
    рантайме, и .exe собирается без единой жалобы, а падает при первом
    обращении к бирже. Отдельно проверяется, что сборка узнаёт себя выпуском:
    если нет, обновление у пользователя пойдёт через git, которого рядом нет.
    """
    import tempfile

    data = tempfile.mkdtemp()
    env = dict(os.environ, BOT_DATA_DIR=data)
    subprocess.run([EXE, '--selftest'], env=env, timeout=300)
    report = os.path.join(data, 'selftest.log')
    text = open(report, encoding='utf-8').read() if os.path.exists(report) else ''
    say(text.strip() or 'самопроверка не оставила отчёта')
    if 'НЕ ЗАГРУЗИЛОСЬ' in text or 'НЕ УЗНАЁТ СЕБЯ' in text:
        raise SystemExit('самопроверка не пройдена — выкладывать нельзя')
    if tag not in text:
        raise SystemExit(f'внутри сборки версия не {tag} — выкладывать нельзя')


def token():
    out = subprocess.run(['git', 'credential', 'fill'], cwd=ROOT,
                         input='protocol=https\nhost=github.com\n\n',
                         capture_output=True, text=True, timeout=60)
    for line in out.stdout.splitlines():
        if line.startswith('password='):
            return line.split('=', 1)[1]
    raise SystemExit('доступ к GitHub в хранилище не найден. Выполните любой '
                     'git push — система спросит вход и запомнит его.')


def call(url, tok, data=None, method=None, ctype='application/json', raw=None):
    body = raw if raw is not None else (
        json.dumps(data).encode('utf-8') if data is not None else None)
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header('Authorization', f'Bearer {tok}')
    request.add_header('Accept', 'application/vnd.github+json')
    request.add_header('User-Agent', 'kraken-release')
    if body is not None:
        request.add_header('Content-Type', ctype)
    with urllib.request.urlopen(request, timeout=600) as resp:
        text = resp.read().decode('utf-8')
    return json.loads(text) if text.strip().startswith(('{', '[')) else {}


def publish(tag):
    slug = repo_slug()
    tok = token()
    who = call(f'{API}/user', tok).get('login')
    say(f'репозиторий {slug}, доступ от {who}')

    try:
        release = call(f'{API}/repos/{slug}/releases/tags/{tag}', tok)
        say(f'выпуск {tag} уже есть — дополняю файлом')
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise SystemExit(f'не удалось проверить выпуск: {exc.code} {exc.reason}')
        notes = subprocess.run(['git', 'tag', '-l', '--format=%(contents)', tag],
                               cwd=ROOT, capture_output=True, text=True,
                               encoding='utf-8').stdout.strip()
        release = call(f'{API}/repos/{slug}/releases', tok, data={
            'tag_name': tag, 'name': tag, 'body': notes or tag,
            'draft': False, 'prerelease': False})
        say(f'выпуск {tag} создан')

    # Файл с тем же именем не даст загрузиться новому.
    for asset in release.get('assets', []):
        if asset['name'] == ASSET:
            call(f'{API}/repos/{slug}/releases/assets/{asset["id"]}', tok,
                 method='DELETE')
            say('прежний файл в этом выпуске удалён')

    say(f'загружаю {ASSET}...')
    with open(EXE, 'rb') as fh:
        blob = fh.read()
    url = release['upload_url'].split('{')[0] + f'?name={ASSET}'
    asset = call(url, tok, raw=blob, ctype='application/octet-stream')
    say('готово: ' + (asset.get('browser_download_url') or '?'))
    latest = call(f'{API}/repos/{slug}/releases/latest', tok)
    say('latest теперь отдаёт: ' + str(latest.get('tag_name')))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = {a for a in sys.argv[1:] if a.startswith('--')}
    if not args:
        raise SystemExit(__doc__.strip().splitlines()[-3].strip())
    tag = args[0]
    if not tag.startswith('v'):
        raise SystemExit('тег должен начинаться с v, например v1.0.12')

    if '--no-build' not in flags:
        write_version(tag)
        build()
    selftest(tag)

    if '--dry-run' in flags:
        say('пробный прогон: сборка проверена, выкладка пропущена')
        return
    publish(tag)


if __name__ == '__main__':
    main()
