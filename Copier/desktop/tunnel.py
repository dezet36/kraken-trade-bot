"""
Туннель SSH до админки копировщика.

Копия проверенного подхода KrakenRemote (control/remote.py торгового бота),
не импорт: окно копировщика не должно зависеть от кода бота. Уроки оттуда
сохранены — у каждого есть свой прецедент в шапке исходника:
  - системный OpenSSH (в т.ч. из Git for Windows), не paramiko;
  - StrictHostKeyChecking=accept-new: первый отпечаток запоминается, смена — отказ;
  - ExitOnForwardFailure: занят порт — падать сразу, а не жить пустым;
  - stderr ssh вычитывается в фоне, иначе ssh встаёт на записи и туннель
    «живой, но мёртвый»;
  - задание Windows KILL_ON_JOB_CLOSE: программа снята — ssh умирает с ней.

Админка слушает на сервере 127.0.0.1:8790 (COPIER_ADMIN_PORT); здесь — 8798,
чтобы не спорить с окном торгового бота (8799).
"""

import collections
import json
import os
import socket
import subprocess
import sys
import threading
import time

LOCAL_PORT = int(os.getenv('COPIER_ADMIN_LOCAL_PORT', 8798))
CONNECT_TIMEOUT = float(os.getenv('COPIER_ADMIN_CONNECT_TIMEOUT', 20))
SETTINGS_NAME = 'copier_admin.json'
STDERR_TAIL = 30


def base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def settings_path():
    return os.path.join(base_dir(), SETTINGS_NAME)


def load_settings():
    try:
        with open(settings_path(), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    """Паролей здесь нет: вход по ключу SSH. Токен админки берётся с сервера по тому же ssh."""
    clean = {k: v for k, v in data.items() if k != 'token'}
    with open(settings_path(), 'w', encoding='utf-8') as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)


def ssh_candidates():
    out = []
    if sys.platform == 'win32':
        system_root = os.environ.get('SystemRoot', r'C:\Windows')
        out.append(os.path.join(system_root, 'System32', 'OpenSSH', 'ssh.exe'))
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                     os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs')):
            if base:
                out.append(os.path.join(base, 'Git', 'usr', 'bin', 'ssh.exe'))
    return out


def ssh_exe():
    for path in ssh_candidates():
        if os.path.exists(path):
            return path
    from shutil import which
    return which('ssh')


def _common(cfg):
    args = ['-o', 'StrictHostKeyChecking=accept-new']
    # Явный known_hosts: ssh из Git ищет его по HOME и спотыкается о кириллицу в
    # пути пользователя («Host key verification failed», 20.09.2026 у бота).
    if cfg.get('known_hosts'):
        args += ['-o', f'UserKnownHostsFile={cfg["known_hosts"]}']
    if cfg.get('key'):
        args += ['-i', cfg['key'], '-o', 'IdentitiesOnly=yes']
    if cfg.get('ssh_port'):
        args += ['-p', str(cfg['ssh_port'])]
    return args


def target(cfg):
    return f"{cfg.get('user') or 'root'}@{cfg['host']}"


def tunnel_command(cfg, ssh):
    remote_port = int(cfg.get('remote_port') or 8790)
    return ([ssh, '-N', '-T', '-o', 'ExitOnForwardFailure=yes',
             '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
            + _common(cfg) + ['-L', f'{LOCAL_PORT}:127.0.0.1:{remote_port}', target(cfg)])


def fetch_token(cfg, env_path='/opt/copier/data/.env', timeout=20):
    """
    Токен админки читается с сервера тем же ssh — на этой машине он не хранится.
    Возвращает (токен, ошибка).
    """
    ssh = ssh_exe()
    if not ssh:
        return '', 'клиент SSH не найден'
    cmd = [ssh, '-T', '-o', 'BatchMode=yes'] + _common(cfg) + [
        target(cfg), f"sed -n 's/^COPIER_ADMIN_TOKEN=//p' {env_path}"]
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
    except Exception as exc:                           # noqa: BLE001
        return '', str(exc)
    token = done.stdout.decode('utf-8', 'replace').strip().strip('"').strip("'")
    if done.returncode != 0 or not token:
        return '', explain(done.stderr.decode('utf-8', 'replace')) or 'токен на сервере не найден'
    return token, ''


def tie_to_parent(process):
    """Задание Windows с KILL_ON_JOB_CLOSE: программа исчезла любым способом — ssh тоже."""
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                        ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.POINTER(ctypes.c_ulong)),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', COUNTERS),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000   # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        handle = kernel.OpenProcess(0x1F0FFF, False, process.pid)
        if not handle:
            return None
        kernel.AssignProcessToJobObject(job, handle)
        kernel.CloseHandle(handle)
        return job
    except Exception:                                  # noqa: BLE001
        return None


def port_open(port, host='127.0.0.1'):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def wait_for_tunnel(process, timeout=CONNECT_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            out = ''
            try:
                out = (process.stderr.read() or b'').decode('utf-8', 'replace')
            except Exception:                          # noqa: BLE001
                pass
            return False, out.strip() or 'клиент SSH завершился без объяснения'
        if port_open(LOCAL_PORT):
            return True, ''
        time.sleep(0.3)
    return False, f'туннель не поднялся за {timeout:.0f} с'


def explain(error):
    low = (error or '').lower()
    if 'permission denied' in low:
        return 'Сервер не принял ключ SSH. Проверьте файл ключа в настройках.'
    if 'could not resolve' in low or 'name or service' in low:
        return 'Адрес сервера не найден. Проверьте имя или IP.'
    if 'connection refused' in low:
        return 'Сервер отказал в соединении: включён ли он и тот ли порт SSH.'
    if 'timed out' in low:
        return 'Сервер не отвечает. Проверьте адрес и порт SSH.'
    if 'host key verification failed' in low:
        return 'Отпечаток сервера изменился. Подключитесь обычным ssh и разберитесь, почему.'
    if 'address already in use' in low or 'cannot listen' in low:
        return f'Порт {LOCAL_PORT} занят — закройте второе окно CopierAdmin.'
    return (error or '').strip()


def drain_stderr(process):
    tail = collections.deque(maxlen=STDERR_TAIL)
    process.stderr_tail = tail
    stream = getattr(process, 'stderr', None)
    if stream is None:
        return None

    def pump():
        try:
            for raw in iter(stream.readline, b''):
                line = raw.decode('utf-8', 'replace').rstrip()
                if line:
                    tail.append(line)
        except Exception:                              # noqa: BLE001
            pass

    thread = threading.Thread(target=pump, daemon=True, name='ssh-stderr')
    thread.start()
    return thread


def open_tunnel(cfg):
    """Поднимает туннель. (процесс, ошибка)."""
    ssh = ssh_exe()
    if not ssh:
        looked = '\n'.join(f'  {p}' for p in ssh_candidates())
        return None, ('Клиент SSH не найден. Искал:\n' + looked + '\nи в PATH.\n\nПоставьте '
                      'Клиент OpenSSH (Параметры → Приложения → Дополнительные компоненты) '
                      'или Git for Windows.')
    if not cfg.get('host'):
        return None, 'Не задан адрес сервера.'
    if port_open(LOCAL_PORT):
        return None, f'Порт {LOCAL_PORT} уже занят — вероятно, программа запущена вторым окном.'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0
    process = subprocess.Popen(tunnel_command(cfg, ssh), stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, creationflags=flags)
    ok, error = wait_for_tunnel(process)
    if not ok:
        try:
            process.terminate()
        except Exception:                              # noqa: BLE001
            pass
        return None, explain(error)
    process._job = tie_to_parent(process)
    drain_stderr(process)
    return process, ''


def url(path=''):
    return f'http://127.0.0.1:{LOCAL_PORT}/{path}'
