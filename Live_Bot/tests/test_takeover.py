"""
После обновления бот обязан подняться сам.

ЧТО БЫЛО. Человек нажимал «Обновить»: файл подменялся, приложение
закрывалось, новая версия стартовала — и не поднималась. Порт дашборда держал
ПРЕЖНИЙ бот, чаще всего запущенный из исходников (`pythonw desktop.py`).
Замок на второй экземпляр такую копию не видит вовсе: у неё свой процесс и
свой замок либо его нет совсем. Порт же один на всех, и упиралось всё в него.

Здесь проверяется различение «своя прежняя копия» и «посторонняя программа».
Разница дорогая в обе стороны: своего надо закрыть, чтобы обновление
завершилось, а постороннего трогать нельзя ни при каких обстоятельствах —
порт мог занять чужой сервер, и снимать его из-за нашего запуска недопустимо.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.parametrize('holder', [
    {'name': 'Kraken.exe', 'cmd': r'C:\app\Kraken.exe'},
    {'name': 'python.exe', 'cmd': r'C:\py\python.exe desktop.py'},
    {'name': 'pythonw.exe', 'cmd': r'C:\py\pythonw.exe desktop.py'},
    {'name': 'python.exe', 'cmd': r'C:\py\python.exe Live_Bot\bot.py'},
    {'name': 'PYTHONW.EXE', 'cmd': r'C:\py\PYTHONW.EXE DESKTOP.PY'},
])
def test_our_copies_are_recognised(holder):
    import desktop

    assert desktop.is_our_bot(holder) is True


@pytest.mark.parametrize('holder', [
    {'name': 'nginx.exe', 'cmd': 'nginx -g daemon'},
    {'name': 'node.exe', 'cmd': 'node server.js'},
    {'name': 'python.exe', 'cmd': r'C:\py\python.exe -m http.server 8787'},
    {'name': '', 'cmd': ''},
    {},
])
def test_strangers_are_left_alone(holder):
    """
    Посторонний процесс не трогаем никогда.

    Особенно третий случай: это python, но НЕ наш бот. Опознавание по одному
    только имени файла закрыло бы чужой сервер, поднятый на том же порту.
    """
    import desktop

    assert desktop.is_our_bot(holder) is False


def test_holder_of_free_port_is_empty():
    import socket

    import desktop

    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    assert desktop.port_holder(port) == {}


def test_holder_of_busy_port_is_found():
    """Занятый порт находит владельца — иначе решать было бы не по чему."""
    import socket

    import desktop

    if sys.platform != 'win32':
        pytest.skip('поиск владельца порта сделан для Windows')

    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        holder = desktop.port_holder(port)
        assert holder.get('pid') == str(os.getpid())
        assert 'python' in (holder.get('name') or '').lower()
    finally:
        server.close()


def test_swap_script_passes_the_flag(tmp_path, monkeypatch):
    """
    Сценарий подмены запускает новую версию С ФЛАГОМ.

    Без флага новая версия спросила бы разрешения закрыть прежнюю копию — а
    спрашивать некого: человек нажал «Обновить» и ждёт результата, а не
    диалога. Именно на этом шаге обновление и застревало.
    """
    import config
    import updater_app

    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(updater_app, 'config', config)
    script = updater_app._swap_script(
        str(tmp_path / 'Kraken.exe'), str(tmp_path / 'Kraken.exe.new'),
        str(tmp_path / 'Kraken.exe.old'))
    body = open(script, encoding='ascii', errors='replace').read()
    assert '--after-update' in body
    assert 'start ""' in body


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
