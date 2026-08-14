"""
Перезапуск после обновления проходит БЕЗ диспетчера задач.

ЖАЛОБА: «когда я обновляю, приложение закрывается но не запускается, пишет
что порты уже заняты. Мне приходится руками заходить в диспетчер задач и
отключать процесс питона». Повторена дважды с промежутком в несколько версий:
первая правка чинила опознание держателя порта, а застревало не только там.

ЧТО НАШЛОСЬ ПРИ РАЗБОРЕ — три места, и каждое само по себе достаточно.

    ЗАМОК НЕВОЗМОЖНО БЫЛО ВЗЯТЬ ПОВТОРНО. CreateMutexW отдаёт дескриптор даже
    когда мьютекс уже существует, а мы в этом случае возвращали False и
    дескриптор бросали открытым. Пока он открыт, объект жив: прежняя копия
    закрыта, а замок «занят» — нами же, навсегда. Любое ожидание «сейчас она
    уйдёт, и я возьму» было обречено. Замерено: пять попыток после гибели
    держателя — все False.

    ЗАМОК ПРОВЕРЯЛСЯ ДО ПОРТА И НЕ ЗНАЛ ПРО ОБНОВЛЕНИЕ. Прежняя копия успевает
    отпустить порт, ещё держа замок: окно закрыто, сервер остановлен, процесс
    доживает секунды. Запуск после подмены файла упирался в замок, до порта не
    доходил и говорил «Программа уже работает» — про копию, которой сейчас не
    станет.

    ОТКАЗ В ОПОЗНАНИИ ЗАКАНЧИВАЛСЯ ТУПИКОМ. Не разобрали netstat — держатель
    пустой, holder['pid'] поднимал KeyError, тот молча превращался в False, и
    человек читал «снимите вручную».

ОБЩЕЕ ПРАВИЛО, РАДИ КОТОРОГО ВСЁ ЭТО: у бота не должно быть состояния, из
которого его вытаскивает человек через диспетчер задач.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import desktop  # noqa: E402
import single_instance  # noqa: E402


def _hold_the_lock(data_dir, seconds=30):
    """Отдельный процесс, который берёт замок и держит его."""
    # Отметка о себе пишется ДО печати: родитель ждёт именно эту строку и
    # сразу идёт читать запись. Наоборот — гонка, и close_previous не находит
    # PID, хотя копия уже работает.
    code = (f"import sys, time; sys.path.insert(0, {ROOT!r});"
            f"import single_instance;"
            f"got = single_instance.acquire({data_dir!r});"
            f"single_instance.mark_running({data_dir!r}, 'v0.0.1', 'test');"
            f"print(got, flush=True);"
            f"time.sleep({seconds})")
    proc = subprocess.Popen([sys.executable, '-c', code],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == 'True'
    return proc


class TestLockCanBeTakenAgain:
    """Замок обязан освобождаться, иначе «закрыть и продолжить» невозможно."""

    def test_lock_frees_after_the_holder_is_gone(self):
        """
        ГЛАВНАЯ ПРОВЕРКА. Раньше здесь было False навсегда: неудачная попытка
        оставляла открытый дескриптор, и он же не давал взять замок потом.
        """
        if sys.platform != 'win32':
            return
        folder = tempfile.mkdtemp(prefix='lock-')
        holder = _hold_the_lock(folder)
        try:
            assert single_instance.acquire(folder) is False
            assert single_instance.acquire(folder) is False   # как в цикле ожидания
        finally:
            holder.kill()
            holder.wait(timeout=10)
        for _ in range(10):
            if single_instance.acquire(folder):
                return
            time.sleep(0.5)
        raise AssertionError('замок не взялся, хотя держать его больше некому')

    def test_close_previous_takes_the_lock(self):
        """Закрыть прежнюю копию по её же записанному PID и забрать замок."""
        if sys.platform != 'win32':
            return
        folder = tempfile.mkdtemp(prefix='lock-')
        holder = _hold_the_lock(folder)
        try:
            assert single_instance.close_previous(folder, wait=20) is True
            holder.wait(timeout=10)
            assert holder.poll() is not None      # прежняя копия закрыта
        finally:
            if holder.poll() is None:
                holder.kill()

    def test_close_previous_without_a_record_just_tries_the_lock(self):
        """Записи нет — никого не убиваем, просто пробуем взять замок."""
        folder = tempfile.mkdtemp(prefix='lock-')
        assert single_instance.close_previous(folder, wait=1) is True


class TestHolderIsRecognisedByItsOwnRecord:
    """
    Опознание не должно зависеть от имени процесса и командной строки.

    Их приходится угадывать, и на сервере они угадывались неверно: запуск из
    исходников выглядит как `python.exe -c ...`, у собранного приложения
    дочерний процесс, wmic из свежих Windows удалён. PID прежняя копия пишет
    о себе сама, в наш каталог, и врать там нечему.
    """

    def test_recorded_pid_counts_as_ours(self, monkeypatch):
        monkeypatch.setattr(desktop, 'our_recorded_pid', lambda: 4242)
        assert desktop.is_our_bot({'name': 'python.exe',
                                   'cmd': 'python manage.py runserver',
                                   'pid': '4242'}) is True

    def test_someone_elses_pid_is_still_someone_else(self, monkeypatch):
        monkeypatch.setattr(desktop, 'our_recorded_pid', lambda: 4242)
        assert desktop.is_our_bot({'name': 'nginx.exe', 'cmd': '',
                                   'pid': '77'}) is False

    def test_our_own_pid_is_not_a_previous_copy(self):
        """Себя закрывать нельзя, а PID в записи может остаться и наш."""
        folder = tempfile.mkdtemp(prefix='rec-')
        single_instance.mark_running(folder, 'v1', 'test')
        old_dir = config.DATA_DIR
        config.DATA_DIR = folder
        try:
            # mark_running записал PID процесса тестов — это мы сами.
            assert desktop.our_recorded_pid() == 0
        finally:
            config.DATA_DIR = old_dir


class TestNoDeadEnd:
    """Ни одна развилка не заканчивается «снимите процесс вручную»."""

    def test_unknown_holder_does_not_raise(self):
        """
        Пустой держатель раньше поднимал KeyError внутри try и превращался в
        молчаливое False. Теперь это ожидание, а не тупик.
        """
        free = _free_port()
        assert desktop.stop_holder({}, free, wait=1) is True   # порт и так свободен

    def test_free_port_is_never_a_reason_to_kill_anything(self, monkeypatch):
        """
        САМАЯ ДОРОГАЯ ОШИБКА ЭТОГО МОДУЛЯ, и поймана она на себе.

        Запасной путь «держателя не назвали — снимаем записанный PID» работал
        даже тогда, когда порт СВОБОДЕН. Прогон тестов раз за разом убивал
        работающее приложение: четыре падения подряд с кодом 1 и без единой
        строки в журнале, потому что taskkill /F следов не оставляет.
        """
        killed = []
        monkeypatch.setattr(desktop, 'our_recorded_pid', lambda: 12345)
        monkeypatch.setattr(desktop.subprocess, 'run',
                            lambda cmd, **k: killed.append(cmd))
        assert desktop.stop_holder({}, _free_port(), wait=1) is True
        assert killed == [], 'на свободном порту никого снимать нельзя'

    def test_unknown_holder_falls_back_to_the_recorded_pid(self, monkeypatch):
        """netstat промолчал — закрываем по записанному PID."""
        killed = {}

        def fake_run(cmd, **kwargs):
            killed['cmd'] = cmd
            return subprocess.CompletedProcess(cmd, 0)

        # Порт СНАЧАЛА занят — иначе закрывать некого и запасной путь не нужен.
        # Порядок ответов: занят при входе, свободен после снятия.
        answers = [True, False]
        monkeypatch.setattr(desktop, 'our_recorded_pid', lambda: 3131)
        monkeypatch.setattr(desktop.subprocess, 'run', fake_run)
        monkeypatch.setattr(desktop, 'port_busy',
                            lambda *a, **k: answers.pop(0) if answers else False)
        assert desktop.stop_holder({}, 8931, wait=1) is True
        assert '3131' in killed['cmd']

    def test_free_port_finds_a_neighbour(self):
        """Порт занят посторонним — работаем на соседнем, а не отказываемся."""
        taken = socket.socket()
        taken.bind(('127.0.0.1', 0))
        taken.listen(5)
        port = taken.getsockname()[1]
        try:
            moved = desktop.free_port(port)
            assert moved and moved != port
            assert desktop.port_busy(moved) is False
        finally:
            taken.close()

    def test_free_port_gives_up_honestly(self, monkeypatch):
        """Свободного нет — возвращаем 0, а не случайный номер."""
        monkeypatch.setattr(desktop, 'port_busy', lambda *a, **k: True)
        assert desktop.free_port(8000, tries=3) == 0


class TestClosingActuallyCloses:
    """
    Закрытое окно обязано означать закрытую программу.

    Возврата из main() для этого НЕ хватает: интерпретатор перед выходом
    дожидается недемонских потоков, а их заводит пул планировщика. Процесс
    оставался в памяти вместе с портом и замком — и следующий запуск упирался
    в «порт занят». Это и есть тот процесс, который приходилось снимать руками.
    """

    def test_shutdown_ends_the_process(self, monkeypatch):
        ended = {}
        monkeypatch.setattr(desktop.os, '_exit', lambda code: ended.setdefault('code', code))
        desktop._shutdown()
        assert ended.get('code') == 0

    def test_shutdown_cancels_polymarket_orders_first(self, monkeypatch):
        """
        Заявки стоят НА БИРЖЕ и переживут закрытие программы: исполнятся без
        нас, а вести полученную позицию будет некому.
        """
        from polymarket import service

        order = []
        alive = {'yes': True}
        monkeypatch.setattr(service, 'status', lambda: {'alive': alive['yes']})

        def fake_stop():
            order.append('остановил маркет-мейкер')
            alive['yes'] = False
            return True

        monkeypatch.setattr(service, 'stop', fake_stop)
        monkeypatch.setattr(desktop.os, '_exit',
                            lambda code: order.append('закрыл процесс'))
        desktop._shutdown()
        assert order == ['остановил маркет-мейкер', 'закрыл процесс']

    def test_shutdown_does_not_hang_forever(self, monkeypatch):
        """Маркет-мейкер не ответил — закрываемся всё равно, но не молча."""
        from polymarket import service

        monkeypatch.setattr(service, 'status', lambda: {'alive': True})
        monkeypatch.setattr(service, 'stop', lambda: True)
        monkeypatch.setattr(desktop, 'SHUTDOWN_TIMEOUT', 2)
        monkeypatch.setattr(desktop.time, 'sleep', lambda s: None)
        said = []
        monkeypatch.setattr(desktop, 'log', said.append)
        ended = {}
        monkeypatch.setattr(desktop.os, '_exit', lambda code: ended.setdefault('code', code))
        desktop._shutdown()
        assert ended.get('code') == 0
        assert any('заявки могли остаться' in str(line) for line in said)


class TestMarketMakerStopsAtOnce:
    """
    Сон между тактами прерываемый: пока поток спит, заявки стоят на бирже.

    Полминуты обычного time.sleep — это полминуты, которые заявки висят без
    присмотра после нажатия «Остановить» или закрытия окна.
    """

    def test_stop_wakes_the_sleeping_loop(self):
        from polymarket import service

        service._wake.clear()
        started = time.time()
        done = []

        def sleeper():
            service._wake.wait(30)
            done.append(time.time() - started)

        worker = __import__('threading').Thread(target=sleeper, daemon=True)
        worker.start()
        time.sleep(0.2)
        service.stop()
        worker.join(timeout=5)
        service._state['stopping'] = False
        service._wake.clear()
        assert done and done[0] < 2, 'остановка ждала конца такта'

    def test_start_clears_the_flag(self):
        """Иначе следующий запуск вышел бы на первом же такте."""
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        assert "_state['stopping'] = False\n    _wake.clear()" in text


class TestStartupWiring:
    """
    Развилки в main() читаются глазами: поднять там окно и мьютекс в тесте
    нечем, а пропажа любой из них возвращает жалобу целиком.
    """

    def setup_method(self):
        self.text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()

    def test_after_update_passes_the_lock_gate(self):
        assert 'if not got_lock and after_update:' in self.text
        assert 'single_instance.close_previous(config.DATA_DIR)' in self.text

    def test_lock_gate_offers_to_take_over(self):
        """Разные версии — предлагаем перейти, а не шлём в диспетчер задач."""
        assert 'Закрыть работающую версию и запустить эту?' in self.text

    def test_foreign_port_moves_instead_of_refusing(self):
        assert 'moved = 0 if ours else free_port(config.DASHBOARD_PORT + 1)' in self.text
        assert "os.environ['DASHBOARD_PORT'] = str(moved)" in self.text
        assert 'config.DASHBOARD_PORT = moved' in self.text

    def test_window_close_goes_through_shutdown(self):
        """Без этого процесс переживал закрытие окна и держал порт."""
        assert '    _open_window(url)\n    _shutdown()' in self.text

    def test_own_copy_alive_still_stops_us(self):
        """
        Две копии на одних файлах хуже, чем незапуск: они затрут журнал
        сделок друг друга. Уходить на соседний порт можно только от чужого.
        """
        assert '0 if ours else' in self.text


def _free_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port
