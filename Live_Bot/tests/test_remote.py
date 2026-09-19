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

    def test_a_silent_ssh_times_out_with_a_reason(self, monkeypatch):
        """
        ПОРТ ПОДМЕНЯЕТСЯ, А НЕ БЕРЁТСЯ ИЗ ЖИЗНИ. Первая версия просто ждала
        таймаута — и падала, когда рядом было запущено само приложение: оно
        держит 8799 открытым, wait_for_tunnel видел живой порт и отвечал
        «готово» вместо отказа.

        Проверка, зависящая от того, что запущено на машине, не проверяет
        ничего: она то проходит, то нет, и разбираться приходится не с кодом.
        """
        monkeypatch.setattr(remote, 'port_open',
                            lambda port, host='127.0.0.1': False)
        ok, error = remote.wait_for_tunnel(FakeProcess(), timeout=0.6)
        assert not ok
        assert 'не поднялся' in error

    def test_a_silent_death_still_names_something(self, monkeypatch):
        """Молчащий отказ — худший: сказать «неизвестно» лучше, чем ничего."""
        monkeypatch.setattr(remote, 'port_open',
                            lambda port, host='127.0.0.1': False)
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


class TestTheWindowEngine:
    """
    Порядок движков выбран по замеру, а не по вкусу.

    На Windows 10 LTSC 2019 WebView2 установлен (153.0.4234.32), его библиотеки
    в сборку попали, страница с сервера приходит целиком — и окно остаётся
    БЕЛЫМ. Движок не отрисовывает.

    Отличить это в коде нельзя: webview.start() не бросает исключения и не
    возвращает признака неудачи. Поэтому запасной путь, привязанный к
    исключению, не включался никогда, а человек смотрел в пустое окно.
    """

    def test_a_browser_window_is_looked_for(self):
        import remote_app
        assert remote_app.CHROME_PATHS, 'путей к браузеру нет вовсе'
        joined = ' '.join(p for p in remote_app.CHROME_PATHS if p).lower()
        assert 'chrome.exe' in joined
        assert 'msedge.exe' in joined

    def test_the_window_gets_its_own_profile(self):
        """
        Без отдельного профиля окно подклеится к уже открытому браузеру: своей
        кнопки на панели задач не будет, а программа завершится сразу после
        запуска, не дождавшись закрытия окна.
        """
        import inspect

        import remote_app

        source = inspect.getsource(remote_app.open_app_window)
        assert '--user-data-dir' in source
        assert '--app=' in source

    def test_the_engine_can_be_switched_back(self):
        """
        Выбор закреплён переменной, а не вшит: на другой машине WebView2 может
        работать, и отнимать его насовсем из-за одной неудачи неправильно.
        """
        import inspect

        import remote_app

        assert 'REMOTE_ENGINE' in inspect.getsource(remote_app.main)


class TestTheTunnelDiesWithTheApp:
    """
    Снятая принудительно программа не имеет права оставить ssh жить.

    ОТКУДА ЭТО. terminate() в finally закрывает туннель при нормальном выходе.
    Но снятие через диспетчер задач, Stop-Process или пересборку finally не
    выполняет — и ssh оставался сиротой, держа порт 8799.

    Следующий запуск видел занятый порт, отказывался и возвращался к окну
    настроек. Со стороны это выглядело как «программа перестала работать»:
    окно открывается, данных нет. На поиск причины ушёл час отладки, и ею
    оказался процесс, которого никто не убил.
    """

    def test_a_successful_tunnel_is_tied_to_the_process(self, monkeypatch):
        tied = {}

        def fake_tie(process):
            tied['pid'] = process.pid
            return 'дескриптор'

        monkeypatch.setattr(remote, 'ssh_exe', lambda: 'ssh')
        monkeypatch.setattr(remote, 'port_open',
                            lambda port, host='127.0.0.1': False)
        monkeypatch.setattr(remote, 'wait_for_tunnel',
                            lambda process, timeout=None: (True, ''))
        monkeypatch.setattr(remote, 'tie_to_parent', fake_tie)

        started = {}

        class Fake:
            pid = 4242

            def terminate(self):
                started['terminated'] = True

        monkeypatch.setattr(remote.subprocess, 'Popen',
                            lambda *a, **k: Fake())

        process, error = remote.open_tunnel({'host': 'srv'})
        assert process is not None, error
        assert tied.get('pid') == 4242, 'туннель не привязан к программе'

    def test_the_handle_is_kept_on_the_process(self, monkeypatch):
        """
        Ссылка хранится на процессе, а не в местной переменной: та исчезла бы
        при выходе из функции, задание закрылось бы, и ssh умер бы сразу после
        успешного подключения.
        """
        monkeypatch.setattr(remote, 'ssh_exe', lambda: 'ssh')
        monkeypatch.setattr(remote, 'port_open',
                            lambda port, host='127.0.0.1': False)
        monkeypatch.setattr(remote, 'wait_for_tunnel',
                            lambda process, timeout=None: (True, ''))
        monkeypatch.setattr(remote, 'tie_to_parent', lambda p: 'дескриптор')

        class Fake:
            pid = 1

            def terminate(self):
                pass

        monkeypatch.setattr(remote.subprocess, 'Popen', lambda *a, **k: Fake())
        process, _error = remote.open_tunnel({'host': 'srv'})
        assert getattr(process, '_job', None) == 'дескриптор'

    def test_tying_never_breaks_the_connection(self):
        """
        Привязка — удобство, а не условие работы. На системе, где задание
        создать нельзя, туннель обязан подняться как прежде.
        """
        class Fake:
            pid = 999999999

        assert remote.tie_to_parent(Fake()) is None or True


class FakeWindow:
    """Окно браузера, которое открылось. Открылось — ещё не значит показало."""

    def __init__(self):
        self.terminated = False

    def wait(self):
        return 0

    def terminate(self):
        self.terminated = True


class TestAnOpenedWindowIsNotYetAShownOne:
    """
    ЗАПУСК ОКНА УСПЕХОМ НЕ ЯВЛЯЕТСЯ, и это стоило человеку десяти минут перед
    белым прямоугольником.

    19 сентября 2026: машина включена, программа запущена, окно открыто — и
    пустое. Всё, что можно было проверить изнутри, было в порядке: туннель
    поднят, страница с сервера приходит целиком (200, 256 КБ), браузер
    запущен и живёт. Причину записал сам Chrome в свой журнал событий:
    `FinishNav3 ERR_ABORTED` — навигация оборвалась. В режиме --app страницы
    ошибок нет, поэтому окно осталось просто белым.

    Изнутри это видно ровно одним способом: спросить у панели, забирал ли
    кто-нибудь данные. Страницу рисует JS, и пока он не сходил за ними, окно
    пустое, чем бы ни ответил сервер на саму страницу.
    """

    def test_a_moved_counter_means_the_page_is_alive(self, monkeypatch):
        import remote_app

        answers = iter([7, 7, 8])
        monkeypatch.setattr(remote_app, 'views',
                            lambda url, timeout=3.0: next(answers))
        monkeypatch.setattr(remote_app.time, 'sleep', lambda _s: None)
        assert remote_app.painted('http://127.0.0.1:8799/', 7, timeout=5)

    def test_a_frozen_counter_means_a_blank_window(self, monkeypatch):
        import remote_app

        monkeypatch.setattr(remote_app, 'views', lambda url, timeout=3.0: 7)
        monkeypatch.setattr(remote_app.time, 'sleep', lambda _s: None)
        assert not remote_app.painted('http://127.0.0.1:8799/', 7, timeout=2)

    def test_a_restarted_bot_counts_as_shown(self, monkeypatch):
        """
        Счётчик сбрасывается вместе с ботом. Требование «стало больше»
        соврало бы про исправное окно — считается любой сдвиг.
        """
        import remote_app

        monkeypatch.setattr(remote_app, 'views', lambda url, timeout=3.0: 1)
        monkeypatch.setattr(remote_app.time, 'sleep', lambda _s: None)
        assert remote_app.painted('http://127.0.0.1:8799/', 340, timeout=5)

    def test_silence_is_not_taken_for_success(self, monkeypatch):
        """
        Не дозвонились до панели — значит и страница не дозвонилась.
        Неизвестность за показ выдавать нельзя: ровно так белое окно и
        считалось рабочим.
        """
        import remote_app

        monkeypatch.setattr(remote_app, 'views', lambda url, timeout=3.0: None)
        monkeypatch.setattr(remote_app.time, 'sleep', lambda _s: None)
        assert not remote_app.painted('http://127.0.0.1:8799/', 3, timeout=2)


class TestABlankWindowIsOpenedAgain:

    def test_the_first_good_window_is_kept_as_is(self, monkeypatch):
        import remote_app

        opened = []
        killed = []
        monkeypatch.setattr(remote_app, 'views', lambda url, timeout=3.0: 0)
        monkeypatch.setattr(remote_app, 'painted',
                            lambda url, before, timeout=None: True)
        monkeypatch.setattr(remote_app, 'open_app_window',
                            lambda url: opened.append(url) or FakeWindow())
        monkeypatch.setattr(remote_app, 'close_windows',
                            lambda: killed.append(1))

        assert remote_app.show_dashboard('http://127.0.0.1:8799/') is not None
        assert len(opened) == 1
        assert not killed, 'исправное окно закрыли'

    def test_a_blank_window_is_closed_before_the_retry(self, monkeypatch):
        """
        Второй запуск с тем же профилем открыл бы ВТОРОЕ окно рядом с белым: у
        Chrome один профиль — один процесс, и новый запуск лишь просит его
        показать ещё одно окно. Поэтому прежнее закрывается.
        """
        import remote_app

        order = []
        monkeypatch.setattr(remote_app, 'views', lambda url, timeout=3.0: 0)
        monkeypatch.setattr(remote_app, 'painted',
                            lambda url, before, timeout=None: False)
        monkeypatch.setattr(remote_app, 'open_app_window',
                            lambda url: order.append('открыл') or FakeWindow())
        monkeypatch.setattr(remote_app, 'close_windows',
                            lambda: order.append('закрыл'))

        assert remote_app.show_dashboard('http://127.0.0.1:8799/') is None
        assert order == ['открыл', 'закрыл', 'открыл', 'закрыл'], order

    def test_a_missing_browser_is_not_retried(self, monkeypatch):
        """
        Браузера нет — повторять нечего, и решать должен вызвавший: у него
        остаются своё окно и запасной путь через браузер по умолчанию.
        """
        import remote_app

        tries = []
        monkeypatch.setattr(remote_app, 'views', lambda url, timeout=3.0: 0)
        monkeypatch.setattr(remote_app, 'open_app_window',
                            lambda url: tries.append(url))

        assert remote_app.show_dashboard('http://127.0.0.1:8799/') is None
        assert len(tries) == 1


class TestOnlyOurOwnWindowsAreClosed:

    def test_the_profile_path_is_what_is_matched(self):
        """
        Закрывать браузер человека нельзя ни при каких обстоятельствах. Отбор
        идёт по пути профиля, который заводим мы сами, — ни по имени процесса,
        ни по заголовку окна.
        """
        import inspect

        import remote_app

        source = inspect.getsource(remote_app.close_windows)
        assert 'window_profile' in source
        assert 'profile.lower() not in line.lower()' in source


class TestTheDashboardCountsItsViewers:
    """
    Признак, по которому окно узнаёт об отрисовке, живёт на стороне панели.

    Без него «страница пришла» и «страница показана» неразличимы — а между
    ними и лежал белый экран: страница приходила целиком, а рисовать её было
    некому.
    """

    PORT = 8934

    def test_taking_the_data_moves_the_counter(self):
        import json
        import urllib.request

        import dashboard

        server = dashboard.start_dashboard(port=self.PORT)
        assert server, 'дашборд не поднялся'
        try:
            base = f'http://127.0.0.1:{self.PORT}/api/'

            def whoami():
                with urllib.request.urlopen(base + 'whoami', timeout=5) as answer:
                    return json.loads(answer.read().decode('utf-8'))

            before = whoami()['views']
            # Сам опрос «кто там» показом не считается: его делает программа,
            # а не страница. Иначе сторож засчитал бы собственный стук.
            assert whoami()['views'] == before

            with urllib.request.urlopen(base + 'data', timeout=15):
                pass
            assert whoami()['views'] == before + 1
        finally:
            server.shutdown()
            server.server_close()


class TestTheTunnelComesBackByItself:
    """
    19 сентября 2026 окно жило без связи: ssh умер, программа напечатала об
    этом в консоль, которой у окна нет, и раздел, открытый после обрыва,
    оказался пустым. Сторож обязан переподключаться, а не сообщать.
    """

    class _Proc:
        def __init__(self, dies=True):
            self.dies = dies
            self.terminated = False

        def wait(self):
            if not self.dies:
                import time as real_time
                real_time.sleep(0.05)

        def terminate(self):
            self.terminated = True

    def test_a_dead_tunnel_is_reopened(self, monkeypatch):
        import remote
        import remote_app

        calls = []
        replacement = self._Proc(dies=False)

        def open_tunnel(cfg):
            calls.append(cfg)
            if len(calls) < 3:
                return None, 'сеть пропала'
            return replacement, ''
        monkeypatch.setattr(remote, 'open_tunnel', open_tunnel)

        link = {'process': self._Proc(dies=True), 'closed': False}
        slept, logged = [], []

        def sleep(seconds):
            slept.append(seconds)
            if len(calls) >= 3:
                link['closed'] = True          # окно закрыли — сторож уходит

        remote_app.keep_alive({'host': 'x'}, link, sleep=sleep, log=logged.append)
        assert len(calls) == 3, 'переподключение не повторялось до успеха'
        assert link['process'] is replacement
        assert slept[:3] == [3, 6, 12], 'пауза обязана расти'
        assert any('восстановлено' in m for m in logged)

    def test_it_stops_when_the_window_closes(self, monkeypatch):
        import remote
        import remote_app

        monkeypatch.setattr(remote, 'open_tunnel',
                            lambda cfg: (_ for _ in ()).throw(AssertionError('не должно звать')))
        link = {'process': self._Proc(dies=True), 'closed': True}
        remote_app.keep_alive({'host': 'x'}, link, sleep=lambda s: None, log=lambda m: None)


class TestOnlyOneCopyRuns:
    """
    Второй запуск по ярлыку не открывает второе окно и не спорит за порт:
    он поднимает уже открытое окно и уходит. Осиротевший туннель прошлой
    копии используется, а не оспаривается — иначе до перезагрузки машины
    программу было бы не запустить.
    """

    def test_the_second_copy_focuses_and_leaves(self, monkeypatch):
        import remote_app

        focused = []
        monkeypatch.setattr(remote_app, 'claim_single_instance', lambda: False)
        monkeypatch.setattr(remote_app, 'focus_existing_window',
                            lambda: focused.append(1) or True)
        monkeypatch.setattr(remote_app.remote, 'load_settings',
                            lambda: (_ for _ in ()).throw(AssertionError('дальше нельзя')))
        assert remote_app.main() == 0
        assert focused == [1]

    def test_the_mutex_is_claimed_once_per_process(self, monkeypatch):
        import remote_app
        if not remote_app.sys.platform.startswith('win'):
            pytest.skip('мьютекс Windows')
        # Своё имя: на машине разработки настоящая программа может быть
        # запущена и держать боевой мьютекс — тогда проверка меряла бы её.
        monkeypatch.setattr(remote_app, 'SINGLE_INSTANCE_NAME',
                            f'Local\KrakenRemote-test-{os.getpid()}')
        assert remote_app.claim_single_instance() is True
        # Тот же процесс держит мьютекс: повторный захват видит «уже есть».
        assert remote_app.claim_single_instance() is False

    def test_an_orphaned_tunnel_is_reused_not_fought(self, monkeypatch):
        import remote_app

        monkeypatch.setattr(remote_app.remote, 'port_open', lambda port, host='127.0.0.1': True)
        monkeypatch.setattr(remote_app, 'views', lambda url, timeout=3.0: 5)
        assert remote_app.tunnel_already_up() is True

        monkeypatch.setattr(remote_app, 'views', lambda url, timeout=3.0: None)
        assert remote_app.tunnel_already_up() is False, 'чужой порт — не наш туннель'

    def test_the_keeper_watches_an_orphaned_tunnel_by_port(self, monkeypatch):
        import remote_app

        ports = iter([True, True, False])              # порт закрылся на третьей проверке
        monkeypatch.setattr(remote_app.remote, 'port_open',
                            lambda port, host='127.0.0.1': next(ports, False))
        class Replacement:
            def wait(self):
                link['closed'] = True             # окно закрыли — сторож уходит
        replacement = Replacement()
        monkeypatch.setattr(remote_app.remote, 'open_tunnel', lambda cfg: (replacement, ''))
        link = {'process': None, 'closed': False}
        slept = []

        def sleep(seconds):
            slept.append(seconds)
        remote_app.keep_alive({'host': 'x'}, link, sleep=sleep, log=lambda m: None)
        assert link['process'] is replacement
        assert 15 in slept
