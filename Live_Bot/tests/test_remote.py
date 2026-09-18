"""
Удалённое окно: соединение, отказы и то, чего оно не должно делать.

ГЛАВНОЕ ЗДЕСЬ — НЕ ТУННЕЛЬ, А ЕГО ОТСУТСТВИЕ. Поднявшийся туннель человек
увидит сам: окно откроется. А вот неподнявшийся обязан объяснить причину, иначе
вместо неё будет пустое окно, и разбираться придётся с логами SSH, которых у
собранной программы нет вовсе.

Второе по важности — что порт панели не выставляется наружу. Открытый порт не
только показывает баланс и историю сделок любому желающему, но и ОТБИРАЕТ
управление: дашборд сам выключает изменение настроек, когда слушает не петлевой
адрес. Туннель тут строго лучше, и проверки стерегут именно это.
"""

import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import remote


class FakeProcess:
    """Процесс ssh, который ведёт себя так, как скажут."""

    def __init__(self, alive=True, error=b''):
        self._alive = alive
        self.stderr = _Reader(error)
        self.terminated = False

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def die(self):
        self._alive = False


class _Reader:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class TestTheTunnelGoesToLoopbackOnly:
    """
    Проброс обязан вести на 127.0.0.1 сервера, а не на его внешний адрес.

    Разница решает всё: панель отдаёт управление только запросам с петлевого
    адреса. Проброс на внешний интерфейс дал бы окно в режиме «только смотреть»
    и заодно оставил бы порт открытым для посторонних.
    """

    def test_the_forward_targets_loopback(self):
        cmd = remote.tunnel_command({'host': 'srv', 'user': 'root'}, 'ssh')
        forward = cmd[cmd.index('-L') + 1]
        assert forward.endswith(':127.0.0.1:8787')
        assert forward.startswith(f'{remote.LOCAL_PORT}:')

    def test_no_command_runs_on_the_server(self):
        """
        -N означает «только проброс». Без него ssh откроет оболочку, и
        программа получила бы возможность выполнять команды на сервере —
        права, которые ей не нужны.
        """
        cmd = remote.tunnel_command({'host': 'srv'}, 'ssh')
        assert '-N' in cmd
        assert cmd[-1].endswith('@srv')

    def test_a_failed_forward_kills_the_connection(self):
        """
        ExitOnForwardFailure: без него ssh живёт при незанятом порте, окно
        открывается и показывает пустоту, а причина теряется.
        """
        cmd = remote.tunnel_command({'host': 'srv'}, 'ssh')
        assert 'ExitOnForwardFailure=yes' in cmd

    def test_a_dead_connection_is_dropped_not_kept(self):
        """
        Мёртвый туннель хуже разорванного: окно продолжает показывать
        последние удачно загруженные числа как текущие.
        """
        cmd = remote.tunnel_command({'host': 'srv'}, 'ssh')
        assert any(x.startswith('ServerAliveInterval') for x in cmd)

    def test_the_local_port_differs_from_the_bot_port(self):
        """
        Если бы проброс шёл на 8787, а на этой машине работал свой бот, окно
        показало бы ЛОКАЛЬНЫЕ данные вместо серверных, ничем не выдав подмены.
        """
        assert remote.LOCAL_PORT != 8787

    def test_the_key_is_passed_not_copied(self):
        cmd = remote.tunnel_command({'host': 'srv', 'key': 'C:/k/id_ed25519'},
                                    'ssh')
        assert '-i' in cmd
        assert cmd[cmd.index('-i') + 1] == 'C:/k/id_ed25519'
        assert 'IdentitiesOnly=yes' in cmd


class TestWaitingForTheTunnel:

    def test_a_dying_ssh_is_noticed_at_once(self):
        """
        Ждать двадцать секунд после смерти ssh — значит заставлять человека
        смотреть на пустое окно вместо причины отказа.
        """
        process = FakeProcess(alive=False, error=b'Permission denied (publickey).')
        started = time.time()
        ok, error = remote.wait_for_tunnel(process, timeout=10)
        assert not ok
        assert 'Permission denied' in error
        assert time.time() - started < 2, 'ждали дольше, чем нужно'

    def test_an_open_port_means_ready(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            old = remote.LOCAL_PORT
            remote.LOCAL_PORT = port
            ok, error = remote.wait_for_tunnel(FakeProcess(), timeout=3)
            assert ok, error
        finally:
            remote.LOCAL_PORT = old
            server.close()

    def test_a_silent_ssh_times_out_with_a_reason(self):
        ok, error = remote.wait_for_tunnel(FakeProcess(), timeout=0.6)
        assert not ok
        assert 'не поднялся' in error

    def test_a_silent_death_still_names_something(self):
        """Молчащий отказ — худший: сказать «неизвестно» лучше, чем ничего."""
        ok, error = remote.wait_for_tunnel(FakeProcess(alive=False, error=b''),
                                           timeout=1)
        assert not ok
        assert error.strip()


class TestRefusalsAreExplained:
    """
    Сообщения клиента SSH точны и бесполезны тому, кто видит их впервые.
    У собранной программы нет ни консоли, ни логов — объяснить некому.
    """

    @pytest.mark.parametrize('raw, expect', [
        ('Permission denied (publickey).', 'ключ'),
        ('ssh: Could not resolve hostname nope', 'не найден'),
        ('connect to host 1.2.3.4 port 22: Connection refused', 'отказал'),
        ('connect to host 1.2.3.4 port 22: Connection timed out', 'не отвечает'),
        ('Host key verification failed.', 'отпечаток'),
        ('bind: Address already in use', 'занят'),
    ])
    def test_known_failures_get_advice(self, raw, expect):
        text = remote.explain(raw).lower()
        assert expect in text
        assert len(text) > len(raw) / 2, 'объяснение не длиннее самой ошибки'

    def test_an_unknown_failure_is_shown_as_is(self):
        """Непонятое сообщение показывается целиком: подменять его нечем."""
        assert remote.explain('нечто небывалое') == 'нечто небывалое'

    def test_nothing_at_all_still_says_something(self):
        assert remote.explain('').strip()


class TestSettings:

    @pytest.fixture(autouse=True)
    def _own_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(remote, 'settings_path',
                            lambda: str(tmp_path / 'remote.json'))

    def test_a_missing_file_is_not_an_error(self):
        assert remote.load_settings() == {}

    def test_a_broken_file_is_not_an_error(self, tmp_path):
        (tmp_path / 'remote.json').write_text('{не json', encoding='utf-8')
        assert remote.load_settings() == {}

    def test_settings_survive_a_round_trip(self):
        remote.save_settings({'host': 'srv', 'user': 'root', 'ssh_port': '22'})
        assert remote.load_settings()['host'] == 'srv'

    def test_no_password_field_exists(self):
        """
        Паролей здесь не хранится: подключение по ключу. Пароль в файле рядом
        с программой рано или поздно уедет вместе с папкой на чужую машину.
        """
        import inspect
        source = inspect.getsource(remote.save_settings)
        assert 'password' not in source.lower()


class TestStartupRefusals:

    def test_without_a_host_it_says_so(self, monkeypatch):
        monkeypatch.setattr(remote, 'ssh_exe', lambda: 'ssh')
        process, error = remote.open_tunnel({'user': 'root'})
        assert process is None
        assert 'адрес' in error.lower()

    def test_without_ssh_it_says_how_to_install(self, monkeypatch):
        monkeypatch.setattr(remote, 'ssh_exe', lambda: None)
        process, error = remote.open_tunnel({'host': 'srv'})
        assert process is None
        assert 'openssh' in error.lower()

    def test_a_busy_port_is_named_before_launching_ssh(self, monkeypatch):
        """
        Второе окно программы — обычная ошибка. Узнать о ней надо до запуска
        ssh, иначе отказ придёт от него и будет говорить про bind.
        """
        monkeypatch.setattr(remote, 'ssh_exe', lambda: 'ssh')
        monkeypatch.setattr(remote, 'port_open', lambda port, host='127.0.0.1': True)
        process, error = remote.open_tunnel({'host': 'srv'})
        assert process is None
        assert 'занят' in error.lower()


class TestFindingTheSshClient:
    """
    Клиент ищется не только в PATH, и это не запасной путь, а основной случай.

    ОТКУДА ЭТО. На Windows 10 LTSC 2019 встроенного OpenSSH нет вовсе, а тот,
    что приходит с Git, лежит в Program Files/Git/usr/bin и в системный PATH
    не добавляется — он виден только внутри Git Bash. Проверка через which
    находила его при запуске из Git Bash и не находила при обычном запуске
    программы, то есть ровно там, где её и запускают.
    """

    def test_the_git_client_is_looked_for(self):
        if sys.platform != 'win32':
            pytest.skip('пути Windows')
        paths = ' '.join(remote.ssh_candidates()).lower()
        assert 'git' in paths, 'клиент из комплекта Git не ищется'
        assert 'openssh' in paths, 'встроенный клиент не ищется'

    def test_the_builtin_comes_first(self):
        """
        Встроенный предпочтительнее: он обновляется вместе с системой, а
        Git-овский — когда человек соберётся обновить Git.
        """
        if sys.platform != 'win32':
            pytest.skip('пути Windows')
        paths = remote.ssh_candidates()
        assert 'OpenSSH' in paths[0]

    def test_an_existing_candidate_wins_over_path(self, tmp_path, monkeypatch):
        fake = tmp_path / 'ssh.exe'
        fake.write_text('', encoding='utf-8')
        monkeypatch.setattr(remote, 'ssh_candidates', lambda: [str(fake)])
        assert remote.ssh_exe() == str(fake)

    def test_the_refusal_names_where_it_looked(self, monkeypatch):
        """
        «Не найден» без списка отправляет ставить компонент Windows человеку,
        у которого клиент уже есть в составе Git, просто в другом месте.
        """
        monkeypatch.setattr(remote, 'ssh_exe', lambda: None)
        monkeypatch.setattr(remote, 'ssh_candidates',
                            lambda: [r'C:\Windows\System32\OpenSSH\ssh.exe',
                                     r'C:\Program Files\Git\usr\bin\ssh.exe'])
        _process, error = remote.open_tunnel({'host': 'srv'})
        assert 'System32' in error and 'Git' in error
        assert 'PATH' in error


class TestPastingWorksOnAnyKeyboardLayout:
    """
    Ctrl+V обязан работать при русской раскладке.

    ОТКУДА ЭТО. Tk привязывает вставку к СИМВОЛУ «v». При русской раскладке
    система сообщает «м», привязки для неё нет, и вставка молча не происходит:
    человек жмёт Ctrl+V, ничего не видит и не понимает почему.

    Адрес сервера и путь к ключу набирать руками — то ещё удовольствие, а
    ошибиться в них легко: опечатка в адресе даёт отказ «сервер не найден», и
    искать причину человек будет где угодно, только не в раскладке.
    """

    def test_the_binding_uses_key_codes_not_letters(self):
        """
        Код физической клавиши от раскладки не зависит: V — это 86 независимо
        от того, какая буква на ней нарисована.
        """
        import remote_app
        assert remote_app._KEY_V == 86
        assert remote_app._KEY_C == 67
        assert remote_app._KEY_X == 88
        assert remote_app._KEY_A == 65

    def test_ctrl_v_is_recognised_by_key_code(self):
        """
        Решение принимается по коду клавиши, поэтому раскладка на него не
        влияет: физическая V имеет код 86 независимо от буквы на ней.
        """
        import remote_app
        assert remote_app.edit_action(remote_app._KEY_V, 0x4) == 'Paste'
        assert remote_app.edit_action(remote_app._KEY_C, 0x4) == 'Copy'
        assert remote_app.edit_action(remote_app._KEY_X, 0x4) == 'Cut'
        assert remote_app.edit_action(remote_app._KEY_A, 0x4) == 'select'

    def test_a_plain_key_is_left_alone(self):
        """Без Ctrl это обычный ввод, и перехватывать его нельзя."""
        import remote_app
        assert remote_app.edit_action(remote_app._KEY_V, 0) is None

    def test_other_combinations_are_not_intercepted(self):
        import remote_app
        assert remote_app.edit_action(70, 0x4) is None      # Ctrl+F

    def test_the_right_click_menu_is_in_russian(self):
        """
        Правая кнопка видна и работает даже когда сочетание перехватила другая
        программа. Пункты на языке остального окна.
        """
        import inspect

        import remote_app

        source = inspect.getsource(remote_app.enable_editing)
        for label in ('Вставить', 'Копировать', 'Выделить всё'):
            assert label in source

    def test_the_handler_stops_the_default_one(self):
        """
        Без 'break' при латинской раскладке сработают обе привязки — наша и
        родная, — и текст вставится дважды. Такую ошибку замечают не сразу.
        """
        import inspect

        import remote_app

        source = inspect.getsource(remote_app.enable_editing)
        assert "return 'break'" in source


class TestTheHostKeyIsHandledWithoutAConsole:
    """
    У программы нет терминала, спросить «yes/no» некому.

    По умолчанию ssh при незнакомом сервере ждёт ответа, а не дождавшись —
    отказывает. Программа честно сообщала «отпечаток не подтверждён» и
    отправляла человека в консоль делать руками ровно то же самое.
    """

    def test_the_first_fingerprint_is_accepted(self):
        cmd = remote.tunnel_command({'host': 'srv'}, 'ssh')
        assert 'StrictHostKeyChecking=accept-new' in cmd

    def test_checking_is_not_turned_off_entirely(self):
        """
        accept-new запоминает отпечаток при первом подключении, но откажет,
        если он ПОМЕНЯЕТСЯ — а смена и означает подмену сервера.

        StrictHostKeyChecking=no молчал бы и при смене. Разница между этими
        двумя значениями — и есть вся защита.
        """
        cmd = remote.tunnel_command({'host': 'srv'}, 'ssh')
        assert 'StrictHostKeyChecking=no' not in cmd
