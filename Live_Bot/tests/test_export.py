"""
Выгрузки работают и в окне приложения, где обычное скачивание не работает.

ЧТО ЗДЕСЬ ЗАКРЕПЛЕНО. Приложение открывается через pywebview с движком
WebView2, и ссылка со свойством `download` там не делает НИЧЕГО: обработчик
загрузок не зарегистрирован, нажатие проходит впустую, файл не появляется
нигде. Со стороны это выглядит как отказ сервера, хотя сервер отвечает 200 с
правильными заголовками — проверено запросом до починки.

Поэтому кнопка выбирает способ по тому, чем открыта страница, и оба способа
обязаны существовать. Тесты держат обе половины: серверную (сохранение на
диск) и клиентскую (различение окна приложения и браузера).
"""

import json
import os
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

PAGE = None


def _page():
    global PAGE
    if PAGE is None:
        with open(os.path.join(ROOT, 'dashboard.html'), encoding='utf-8') as fh:
            PAGE = fh.read()
    return PAGE


class TestSaveEndpoint:

    @pytest.fixture(scope='class')
    def server(self):
        import dashboard
        port = 8931
        threading.Thread(
            target=lambda: dashboard.start_dashboard(port=port, host='127.0.0.1'),
            daemon=True).start()
        time.sleep(2.0)
        return f'http://127.0.0.1:{port}'

    @staticmethod
    def _ensure_journals():
        """
        Журналы создаются ПЕРЕД КАЖДЫМ обращением, а не один раз на класс.

        Прогон тестов выдаёт каждому тесту свою временную папку данных, и файлы,
        созданные для первого, для второго уже лежат не там. Сервер тогда честно
        отвечает 404 «история пуста» — верное поведение, на котором работу
        выгрузки проверить нельзя.
        """
        import dashboard
        csv_path, jsonl_path = dashboard.export_paths()
        for path, body in ((csv_path, 'trade_id,strategy\n1,FIBO\n'),
                           (jsonl_path, '{"trade_id": 1}\n')):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(body)

    def _get(self, url):
        with urllib.request.urlopen(url, timeout=25) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))

    def test_every_kind_lands_on_disk(self, server, tmp_path):
        """
        Каждая выгрузка пишется файлом и отвечает путём к нему.

        Путь возвращается АБСОЛЮТНЫЙ: относительный человек не найдёт, а
        именно поиск файла и был исходной жалобой.
        """
        self._ensure_journals()
        for kind in ('csv', 'jsonl', 'report'):
            status, data = self._get(f'{server}/api/export/save?kind={kind}')
            assert status == 200, kind
            assert data.get('ok') is True, kind
            assert data['bytes'] > 0, kind
            assert os.path.isabs(data['path']), kind
            assert os.path.exists(data['path']), kind

    def test_unknown_kind_falls_back_to_csv_not_a_crash(self, server):
        """Незнакомый вид не роняет сервер: он и так вызывается кнопкой."""
        self._ensure_journals()
        status, data = self._get(f'{server}/api/export/save?kind=nonsense')
        assert status == 200 and data.get('ok') is True

    def test_plain_download_still_works_for_browsers(self, server):
        """Прежний путь скачивания остаётся: в браузере он и нужен."""
        self._ensure_journals()
        for path, kind in (('/api/export.csv', 'csv'),
                           ('/api/export.jsonl', 'jsonl'),
                           ('/api/report.txt', 'report')):
            with urllib.request.urlopen(server + path, timeout=25) as resp:
                assert resp.status == 200, kind
                disp = resp.headers.get('Content-Disposition') or ''
                assert 'attachment' in disp, kind


class TestClientChoosesTheRightWay:

    def test_buttons_are_not_plain_download_links(self):
        """
        Ссылок со свойством download в разметке больше нет.

        Именно они и не работали в окне приложения. Вернуть их — значит вернуть
        поломку, причём молча: в браузере всё будет выглядеть исправно.
        """
        page = _page()
        assert 'href="/api/export.csv" download' not in page
        assert 'href="/api/export.jsonl" download' not in page
        assert 'href="/api/report.txt" download' not in page

    def test_all_three_buttons_exist(self):
        page = _page()
        for kind in ('csv', 'jsonl', 'report'):
            assert f'data-export="{kind}"' in page, kind

    def test_app_window_is_detected_by_pywebview_object(self):
        """
        Окно приложения различается по объекту pywebview, а не по строке агента.

        Агент подделывается и меняется от версии к версии; объект есть ровно
        тогда, когда есть мост pywebview, то есть когда скачивание не сработает.
        """
        page = _page()
        assert "typeof window.pywebview !== 'undefined'" in page
        assert 'navigator.userAgent' not in page.split('function inAppWindow')[1][:400]

    def test_both_paths_are_wired(self):
        """И сохранение на диск, и скачивание через blob присутствуют."""
        page = _page()
        assert '/api/export/save?kind=' in page
        assert 'URL.createObjectURL' in page
        assert 'if (inAppWindow()) saveExport(kind); else downloadExport(kind);' in page

    def test_failure_is_shown_not_swallowed(self):
        """Неудача сообщается человеку, а не гасится в консоли."""
        page = _page()
        assert 'не сохранилось: ' in page
        assert 'не скачалось: ' in page
