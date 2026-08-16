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


class TestTheBrowserWindowComesFirst:

    def _source(self):
        return open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()

    def test_the_app_window_is_tried_before_the_native_one(self):
        text = self._source()
        spot = text.index('def _open_window(')
        block = text[spot:spot + 2600]
        assert block.index('_open_app_window(url)') < block.index('_open_native(url)'), \
            'нативное окно молча не открывается — оно не может идти первым'

    def test_the_native_window_is_still_a_fallback(self):
        """На машинах без Chrome оно единственное, что есть."""
        text = self._source()
        spot = text.index('def _open_window(')
        assert '_open_native(url)' in text[spot:spot + 2600]

    def test_the_plain_browser_is_the_last_resort(self):
        text = self._source()
        spot = text.index('def _open_window(')
        block = text[spot:spot + 3000]
        assert 'webbrowser.open' in block

    def test_the_reason_is_written_down(self):
        """
        Молчаливая поломка обязана быть описана там, где принято решение:
        иначе следующий читатель вернёт «настоящее окно» обратно наверх.
        """
        text = self._source()
        spot = text.index('def _open_window(')
        block = text[spot:spot + 2600]
        assert 'CreateCoreWebView2ControllerAsync' in block
        assert 'не возвращает ошибки' in block


class TestEveryPathNamesItself:
    """
    Разбор «почему нет окна» занял час именно потому, что журнал молчал.
    Приложение работало, дашборд отвечал, а какой из трёх путей выбран и чем он
    кончился — узнать было неоткуда.
    """

    def test_each_branch_writes_a_line(self):
        text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        spot = text.index('def _open_window(')
        block = text[spot:spot + 3200]
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


class TestAnInstantExitIsNotAClosedWindow:
    """
    Chrome с чужим или испорченным профилем выходит сразу, ничего не показав.
    Прежде это читалось как «человек закрыл окно», и программа честно
    останавливала бота: в журнале три строки одной секундой — открыл, закрыл,
    остановился. Снаружи это выглядит как «запустил, и оно само выключилось».

    Живой человек не успевает закрыть окно за пару секунд.
    """

    def _source(self):
        return open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()

    def test_a_quick_exit_falls_through(self):
        text = self._source()
        spot = text.index('def _open_window(')
        block = text[spot:spot + 3600]
        assert 'WINDOW_ALIVE_SECONDS' in block
        assert 'TimeoutExpired' in block

    def test_surviving_the_threshold_means_the_window_is_real(self):
        """Дожил до порога — значит окно показано, ждём закрытия по-настоящему."""
        text = self._source()
        spot = text.index('def _open_window(')
        block = text[spot:spot + 3600]
        assert 'открыто окном браузера' in block
        assert block.index('TimeoutExpired') < block.index('открыто окном браузера')

    def test_the_threshold_is_short_but_not_zero(self):
        import re
        text = self._source()
        got = re.search(r'WINDOW_ALIVE_SECONDS = (\d+)', text)
        assert got and 2 <= int(got.group(1)) <= 10
