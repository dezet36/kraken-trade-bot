"""
Панель обновления обязана объяснять, что работает, даже когда обновить нельзя.

ЗАЧЕМ. Человек скачал новую версию, запустил, а в разделе обновления увидел
одну строку: «каталог не является git-репозиторием». По ней нельзя понять
ничего из того, что нужно: какая версия работает, откуда она запущена и
почему считает себя запущенной из исходников. Разбор превратился в переписку.

Отдельно проверяется отметка о работающем экземпляре: без неё второй запуск
поднимал чужое окно и молча выходил — и новая версия «открывалась» старой.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_unavailable_status_says_what_is_running(monkeypatch):
    import updater

    monkeypatch.setattr(updater, 'is_repo', lambda: False)
    monkeypatch.setattr(updater, '_app_mode', lambda: None)
    info = updater.status(fetch=False)

    assert info['available'] is False
    # Версия и путь обязаны быть — ради них проверка и существует.
    assert info.get('current', {}).get('commit')
    assert info.get('app_dir')
    assert 'frozen' in info
    assert info['reason'] and len(info['reason']) > 40


def test_unavailable_status_hints_at_wrong_exe(monkeypatch):
    """
    Если процесс не собранный — подсказать, что работает не Kraken.exe.

    Это и есть самая частая причина: щёлкнули по новому файлу, а поднялась
    работающая старая копия.
    """
    import updater

    monkeypatch.setattr(updater, 'is_repo', lambda: False)
    monkeypatch.setattr(updater, '_app_mode', lambda: None)
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    info = updater.status(fetch=False)
    assert 'диспетчере задач' in info['reason']


def test_app_mode_survives_missing_module(monkeypatch):
    """
    Пропавший в сборке updater_app не должен уводить на git-ветку.

    Молчаливый переход туда и давал ту самую строку про репозиторий у
    собранного приложения — то есть сообщение, объясняющее не то.
    """
    import builtins

    import updater

    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == 'updater_app':
            raise ImportError('нет в сборке')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake)
    assert updater._app_mode() is None      # не падает


def test_running_mark_round_trip(tmp_path):
    import single_instance

    single_instance.mark_running(str(tmp_path), 'v1.0.6', r'C:\x\Kraken.exe')
    info = single_instance.running_info(str(tmp_path))
    assert info['version'] == 'v1.0.6'
    assert info['exe'].endswith('Kraken.exe')
    assert info['pid'] == os.getpid()


def test_running_mark_absent_is_not_an_error(tmp_path):
    """Нет отметки — второй запуск всё равно обязан показать сообщение."""
    import single_instance

    assert single_instance.running_info(str(tmp_path)) == {}


def test_running_mark_never_raises(tmp_path):
    """Отметка — удобство, а не условие работы: сломаться она не вправе."""
    import single_instance

    single_instance.mark_running(str(tmp_path / 'нет-такой-папки'), 'v1', 'x')
    assert single_instance.running_info(str(tmp_path / 'нет-такой-папки')) == {}


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))


def test_version_file_is_enough_to_be_a_release(monkeypatch, tmp_path):
    """
    Наличие файла VERSION само по себе означает «это выпуск».

    Признак прямее, чем sys.frozen: в репозитории VERSION нет — он создаётся
    только при сборке. У пользователя собранное приложение всё равно уходило
    в git-ветку и сообщало «каталог не является git-репозиторием», значит на
    один sys.frozen полагаться нельзя.
    """
    import updater
    import updater_app

    monkeypatch.setattr(updater_app, 'is_frozen', lambda: False)
    monkeypatch.setattr(updater_app, 'current_version', lambda: 'v1.0.9')
    assert updater._app_mode() is updater_app


def test_no_version_and_not_frozen_is_source(monkeypatch):
    """Запуск из исходников остаётся запуском из исходников."""
    import updater
    import updater_app

    monkeypatch.setattr(updater_app, 'is_frozen', lambda: False)
    monkeypatch.setattr(updater_app, 'current_version', lambda: '')
    assert updater._app_mode() is None
