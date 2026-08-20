"""
Окно приложения: почему браузер идёт первым, а не «настоящее нативное».

ПОЛОМКА, НАЙДЕННАЯ НА ЖИВОЙ МАШИНЕ. Первым шёл pywebview. Окно WinForms он
создаёт, а движок внутри падает:

    CoreWebView2Environment.CreateCoreWebView2ControllerAsync → исключение

Беда в том, КАК он падает. Исключение съедается внутри асинхронной
инициализации, webview.start() не возвращает ошибки и блокируется навсегда.
Снаружи: приложение запущено, дашборд отвечает по сети, окна нет, в журнале ни
строчки. Человек трижды запускал .exe и трижды не понимал, что происходит.

Запасной путь был готов и не срабатывал — до него просто не доходило.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _window_function():
    """
    Тело `_open_window` целиком.

    Здесь резали фиксированное число символов от начала функции, и проверки
    ломались, стоило функции подрасти: «_open_native не найден» означало не
    пропажу запасного пути, а лишний абзац комментария выше. Границу задаёт
    следующее определение верхнего уровня, а не счёт знаков.
    """
    text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
    start = text.index('def _open_window(')
    end = text.index('\ndef ', start + 10)
    return text[start:end]


class TestTheBrowserWindowComesFirst:

    def _source(self):
        return open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()

    def test_the_app_window_is_tried_before_the_native_one(self):
        block = _window_function()
        assert block.index('_open_app_window(url)') < block.index('_open_native(url)'), \
            'нативное окно молча не открывается — оно не может идти первым'

    def test_the_native_window_is_still_a_fallback(self):
        """На машинах без Chrome оно единственное, что есть."""
        assert '_open_native(url)' in _window_function()

    def test_the_plain_browser_is_the_last_resort(self):
        block = _window_function()
        assert 'webbrowser.open' in block

    def test_the_reason_is_written_down(self):
        """
        Молчаливая поломка обязана быть описана там, где принято решение:
        иначе следующий читатель вернёт «настоящее окно» обратно наверх.
        """
        block = _window_function()
        assert 'CreateCoreWebView2ControllerAsync' in block
        assert 'не возвращает ошибки' in block


class TestEveryPathNamesItself:
    """
    Разбор «почему нет окна» занял час именно потому, что журнал молчал.
    Приложение работало, дашборд отвечал, а какой из трёх путей выбран и чем он
    кончился — узнать было неоткуда.
    """

    def test_each_branch_writes_a_line(self):
        block = _window_function()
        assert block.count('log(') >= 4, 'каждый путь обязан назвать себя'
        assert 'пробую окно браузера' in block
        assert 'открыто окном браузера' in block
        assert 'пробую своё окно' in block


class TestTheBuildCarriesTheWindowParts:
    """Потерянный модуль — единственная поломка, которой славится упаковка."""

    def test_the_selftest_asks_for_clr(self):
        """
        pywebview рисует окно через pythonnet. Его в списке не было, и сборка
        проходила самопроверку с неработающим окном.
        """
        text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        spot = text.index('def selftest(')
        assert "'clr'" in text[spot:spot + 1800]

    def test_both_builds_bundle_it(self):
        here = open(os.path.join(ROOT, 'build_exe.ps1'),
                    encoding='utf-8-sig').read()
        assert '--hidden-import clr `' in here
        ci = os.path.join(os.path.dirname(ROOT), '.github', 'workflows',
                          'build-exe.yml')
        if os.path.exists(ci):
            assert '--hidden-import clr `' in open(ci, encoding='utf-8').read(), \
                'без этого следующий выпуск повторит поломку'


class TestTheBuildRefusesEarlyWhenTheAppIsRunning:
    """
    Windows не даёт заменить работающий .exe. Сборка доходит до последнего
    шага, упирается в блокировку и возвращает код 1 — потратив ровно столько
    же времени, сколько удачная, а причина теряется среди сотен строк INFO.

    Так и вышло: приложение было запущено поверх идущей сборки, пять минут
    ушли впустую. Отказ должен приходить сразу и называть причину.
    """

    SCRIPT = open(os.path.join(ROOT, 'build_exe.ps1'), encoding='utf-8-sig').read()

    def test_the_check_exists(self):
        assert 'Get-Process Kraken' in self.SCRIPT

    def test_it_refuses_rather_than_warns(self):
        spot = self.SCRIPT.index('Get-Process Kraken')
        assert 'throw' in self.SCRIPT[spot:spot + 400], (
            'предупреждение не спасёт: сборка всё равно упрётся в блокировку')

    def test_it_runs_before_pyinstaller_starts(self):
        """Смысл проверки — в том, чтобы не тратить пять минут впустую."""
        assert (self.SCRIPT.index('Get-Process Kraken')
                < self.SCRIPT.index('python -m PyInstaller'))

    def test_the_message_says_what_to_do(self):
        spot = self.SCRIPT.index('Get-Process Kraken')
        block = self.SCRIPT[spot:spot + 400]
        assert 'Закрой приложение' in block


class TestTheStartupWaitPicksTheLightPage:
    """
    ПОЧЕМУ ОКНА НЕ БЫЛО ВООБЩЕ — настоящая причина, найденная последней.

    Перед окном стоит ожидание дашборда, и ждало оно `/api/data` — страницу,
    которая считает индикаторы по двум десяткам пар. Замер на запуске:

        /api/whoami   ответил через 13 секунд
        /api/data     ответил через 49 секунд
        порог                       40 секунд

    Девяти секунд не хватило, и дальше шло по худшему пути: показывалось окно
    с ошибкой — в оконной сборке НЕВИДИМОЕ, — и приложение вставало на нём
    навсегда. Снаружи: бот торгует, дашборд отвечает, окна нет, журнал молчит.
    """

    def _source(self):
        return open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()

    def test_the_wait_asks_the_cheap_page(self):
        text = self._source()
        assert "_wait_for_dashboard(url + 'api/whoami')" in text, \
            'ждать тяжёлую страницу значит не дождаться'

    def test_the_heavy_page_is_not_the_gate(self):
        text = self._source()
        assert "_wait_for_dashboard(url + 'api/data')" not in text

    def test_the_timeout_has_room(self):
        import re
        text = self._source()
        got = re.search(r'STARTUP_TIMEOUT = (\d+)', text)
        assert got and int(got.group(1)) >= 60, \
            'запас нужен: на холодном старте сервер поднимается 13 секунд'

    def test_giving_up_is_written_to_the_log_first(self):
        """
        Окно с ошибкой в оконной сборке может не показаться вовсе. Причина
        обязана попасть в журнал ДО него, иначе её негде прочесть.
        """
        text = self._source()
        spot = text.index("_wait_for_dashboard(url + 'api/whoami')")
        block = text[spot:spot + 700]
        assert 'log(' in block
        assert block.index('log(') < block.index('_alert(')

    def test_the_measurement_is_written_where_it_acts(self):
        text = self._source()
        spot = text.index('def _wait_for_dashboard')
        block = text[spot:spot + 1400]
        assert '/api/whoami' in block and '49 секунд' in block


class TestWeWaitForTheWindowNotForTheLauncher:
    """
    Chrome может ПЕРЕДАТЬ работу другому процессу того же профиля и сразу
    выйти. Окно живёт своей жизнью, а наш Popen завершается за доли секунды.

    Пока ждали Popen, это читалось как «человек закрыл окно», и программа
    останавливала бота: в журнале три строки одной секундой — открыл, закрыл,
    остановился. Снаружи — «запустил, и оно само выключилось».

    Признак «окно есть» — живой браузер, держащий НАШ профиль: он переживает
    передачу работы и исчезает ровно тогда, когда окно закрыли.
    """

    def _source(self):
        return open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()

    def test_the_window_is_judged_by_the_profile(self):
        block = _window_function()
        assert '_wait_for_browser_window()' in block
        assert '_wait_while_browser_window()' in block

    def test_the_check_looks_at_the_profile_directory(self):
        text = self._source()
        spot = text.index('def _browser_window_alive(')
        block = text[spot:spot + 1200]
        assert "'app_window'" in block

    def test_an_unanswerable_check_does_not_guess(self):
        """Спросить нечем — считаем, что окно есть: иначе закроемся зря."""
        text = self._source()
        spot = text.index('def _browser_window_alive(')
        assert 'return None' in text[spot:spot + 1200]
        spot2 = text.index('def _wait_for_browser_window(')
        assert 'is None' in text[spot2:spot2 + 700]

    def test_one_disappearance_is_not_a_close(self):
        """Браузер перезапускает свои процессы; закрытие — это устойчивая пропажа."""
        text = self._source()
        spot = text.index('def _wait_while_browser_window(')
        assert 'missing >= 3' in text[spot:spot + 800]


class TestClosingTheWindowIsAsked:
    """
    ДИАЛОГ БЫЛ НАПИСАН И НЕ ВЫЗЫВАЛСЯ НИКОГДА.

    `_confirm_close` привязан внутри ветки СВОЕГО окна, а приложение
    открывается браузером — та ветка пробуется первой и почти всегда
    срабатывает. То есть предупреждение «бот ведёт N позиций» не показывалось
    ни разу: 16 августа окно закрыли при шести открытых позициях, в журнале
    «Окно: закрыто пользователем» — и никакого вопроса.

    Закрытие окна прекращает ведение позиций: перевод в безубыток, частичные
    фиксации, выход по времени. Человек, закрывший окно случайно, об этом не
    знает.
    """

    def test_the_browser_branch_asks_too(self):
        block = _window_function()
        assert '_ask_after_browser_close()' in block, (
            'ветка браузера закрывается молча — именно так предупреждение '
            'не показывалось ни разу')

    def test_it_asks_before_giving_up(self):
        """Сначала вопрос, потом возврат: иначе спрашивать уже не у кого."""
        block = _window_function()
        assert block.index('_ask_after_browser_close()') < block.index('return')

    def test_agreeing_reopens_the_window(self):
        """
        «Продолжить» без окна оставило бы бота работать вслепую: остановить
        его было бы нечем, кроме диспетчера задач.
        """
        block = _window_function()
        spot = block.index('_ask_after_browser_close()')
        assert '_open_app_window(url)' in block[spot:]

    def test_the_question_names_the_stakes(self):
        text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        spot = text.index('def _ask_after_browser_close')
        block = text[spot:text.index('\ndef ', spot + 10)]
        assert 'позиций' in block
        assert 'безубыток' in block, 'человек должен знать, что именно прекратится'

    def test_no_positions_means_no_question(self):
        """Пустой счёт — закрывать нечего, и спрашивать не о чем."""
        text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        spot = text.index('def _ask_after_browser_close')
        block = text[spot:text.index('\ndef ', spot + 10)]
        assert 'if not count:' in block

    def test_a_failed_dialog_stops_rather_than_guesses(self):
        """
        Не смогли спросить — ведём себя как раньше и останавливаемся. Оставить
        бота работать без окна значило бы решить за человека молча.
        """
        text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        spot = text.index('def _ask_after_browser_close')
        block = text[spot:text.index('\ndef ', spot + 10)]
        assert 'except Exception' in block and 'return False' in block
