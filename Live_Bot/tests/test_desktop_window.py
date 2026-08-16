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
