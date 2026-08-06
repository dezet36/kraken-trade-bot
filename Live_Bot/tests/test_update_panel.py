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


def test_version_file_decides_only_outside_a_repo(monkeypatch):
    """
    VERSION решает, только когда git-репозитория рядом НЕТ.

    Признак нужен на случай распакованной сборки, у которой почему-то не
    встал sys.frozen. Но применять его безоговорочно нельзя: после локальной
    сборки файл VERSION остаётся в рабочем каталоге, и запуск из исходников
    начинал считать себя выпуском — обновление предлагало скачать .exe
    поверх рабочей копии с git-историей. Ровно это я и сломал с первой
    попытки, десятью упавшими проверками.
    """
    import updater
    import updater_app

    monkeypatch.setattr(updater_app, 'is_frozen', lambda: False)
    monkeypatch.setattr(updater_app, 'current_version', lambda: 'v1.0.9')

    monkeypatch.setattr(updater, 'is_repo', lambda: False)
    assert updater._app_mode() is updater_app, 'вне репозитория VERSION решает'

    monkeypatch.setattr(updater, 'is_repo', lambda: True)
    assert updater._app_mode() is None, 'в репозитории побеждает git'


def test_frozen_wins_over_repo(monkeypatch):
    """Собранное приложение — выпуск всегда, что бы ни лежало рядом."""
    import updater
    import updater_app

    monkeypatch.setattr(updater_app, 'is_frozen', lambda: True)
    monkeypatch.setattr(updater, 'is_repo', lambda: True)
    assert updater._app_mode() is updater_app


def test_no_version_and_not_frozen_is_source(monkeypatch):
    """Запуск из исходников остаётся запуском из исходников."""
    import updater
    import updater_app

    monkeypatch.setattr(updater_app, 'is_frozen', lambda: False)
    monkeypatch.setattr(updater_app, 'current_version', lambda: '')
    monkeypatch.setattr(updater, 'is_repo', lambda: False)
    assert updater._app_mode() is None


@pytest.mark.parametrize('written', [
    'v1.0.9',                     # чисто
    '\ufeffv1.0.9',               # с меткой порядка байт
    'v1.0.9\n',                   # с переводом строки
    '\ufeffv1.0.9\r\n',           # и то и другое, как пишет Windows
    '  v1.0.9  ',                 # с пробелами
])
def test_version_is_read_clean(written, tmp_path, monkeypatch):
    """
    Версия читается без мусора, чем бы файл ни был записан.

    Сравнение с выпуском строгое: 'v1.0.9' != '\ufeffv1.0.9'. Метка порядка
    байт, которую на Windows добавляет почти всё — PowerShell, блокнот, —
    невидима в тексте, но заставляла приложение вечно предлагать обновление
    на уже установленную версию.
    """
    import updater_app

    (tmp_path / 'VERSION').write_text(written, encoding='utf-8')
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    assert updater_app.current_version() == 'v1.0.9'


def test_same_version_means_up_to_date(tmp_path, monkeypatch):
    """Сборка с BOM обязана считать себя актуальной, а не отстающей."""
    import updater_app

    (tmp_path / 'VERSION').write_text('\ufeffv1.0.9', encoding='utf-8')
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(updater_app, '_fetch_latest',
                        lambda: {'tag_name': 'v1.0.9', 'assets': []})
    info = updater_app.status(fetch=True)
    assert info['behind'] == 0
    assert info['can_update'] is False
    assert 'последняя' in info['reason']
