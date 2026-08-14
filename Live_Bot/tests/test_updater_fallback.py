"""
Обновление переживает отказ API GitHub.

ПОЧЕМУ ЭТО ВАЖНО. У API предел в шестьдесят обращений в час НА АДРЕС, и
считается он без разбора: на сервере с общим или облачным адресом его
исчерпывает кто угодно. Бот отвечал «не удалось связаться с GitHub: HTTP Error
403» — выглядит как поломка сети, хотя сеть в порядке и файл выпуска лежит на
месте. Обновиться при этом было нельзя.

Обычная страница /releases/latest предела не имеет и называет версию
перенаправлением. Описания выпуска она не даёт, поэтому идёт вторым номером.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import updater_app  # noqa: E402


class Refused(Exception):
    code = 403


class TestFallbackSavesTheUpdate:

    def test_api_failure_falls_back_to_the_page(self, monkeypatch):
        """Отказ API не должен лишать возможности обновиться."""
        monkeypatch.setattr(updater_app, 'is_frozen', lambda: True)
        monkeypatch.setattr(updater_app, 'current_version', lambda: 'v1.0.1')
        monkeypatch.setattr(updater_app, '_fetch_latest',
                            lambda: (_ for _ in ()).throw(Refused()))
        monkeypatch.setattr(updater_app, '_fetch_latest_without_api',
                            lambda: {'tag_name': 'v1.0.36', 'name': 'v1.0.36',
                                     'published_at': '', 'body': '',
                                     'assets': [{'name': 'Kraken.exe',
                                                 'size': 0,
                                                 'browser_download_url': 'u'}]})
        out = updater_app.status()
        assert out['behind'] == 1, 'обновление видно даже без API'
        assert out['can_update'] is True

    def test_both_paths_down_says_what_happened(self, monkeypatch):
        """
        Когда не работает ничего, причина называется по-человечески, а не
        номером ошибки: «403» не подсказывает, что делать, а «предел в 60
        обращений в час, через час пройдёт само» — подсказывает.
        """
        monkeypatch.setattr(updater_app, 'is_frozen', lambda: True)
        monkeypatch.setattr(updater_app, 'current_version', lambda: 'v1.0.1')
        monkeypatch.setattr(updater_app, '_fetch_latest',
                            lambda: (_ for _ in ()).throw(Refused()))
        monkeypatch.setattr(updater_app, '_fetch_latest_without_api',
                            lambda: (_ for _ in ()).throw(OSError('сети нет')))
        out = updater_app.status()
        assert out['can_update'] is False
        assert 'предел' in out['reason'], out['reason']
        assert '403' not in out['reason'], 'номер ошибки ничего не объясняет'

    def test_api_success_is_preferred(self, monkeypatch):
        """
        Пока API отвечает, берём его: только он даёт описание выпуска, а
        предлагать обновление без объяснения значит просить доверять вслепую.
        """
        called = []
        monkeypatch.setattr(updater_app, 'is_frozen', lambda: True)
        monkeypatch.setattr(updater_app, 'current_version', lambda: 'v1.0.1')
        monkeypatch.setattr(updater_app, '_fetch_latest',
                            lambda: {'tag_name': 'v1.0.36', 'name': 'имя',
                                     'published_at': '2026-08-14', 'body': 'что нового',
                                     'assets': [{'name': 'Kraken.exe', 'size': 1,
                                                 'browser_download_url': 'u'}]})
        monkeypatch.setattr(updater_app, '_fetch_latest_without_api',
                            lambda: called.append(1))
        out = updater_app.status()
        assert called == [], 'запасной путь не трогаем без нужды'
        assert out['pending'][0]['notes'] == 'что нового'


class TestExplanation:

    def test_rate_limit_is_named_and_dated(self):
        text = updater_app._explain(Refused())
        assert 'предел' in text and 'час' in text

    def test_missing_release_is_named(self):
        class Gone(Exception):
            code = 404
        assert 'не найден' in updater_app._explain(Gone())

    def test_other_errors_keep_their_text(self):
        assert 'сети нет' in updater_app._explain(OSError('сети нет'))
