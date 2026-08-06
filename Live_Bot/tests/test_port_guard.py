"""
Занятый порт: приложение обязано отказаться, а не показать чужой дашборд.

ЧТО БЫЛО. Готовность проверялась запросом к http://127.0.0.1:8787/api/data,
и на этот запрос успешно отвечал ЧУЖОЙ сервер, если он уже слушал порт.
Приложение считало, что поднялось, и открывало окно на дашборде другого
процесса. Со стороны: «запустил новую версию, а в окне старая».

Именно так и вышло у пользователя: семь часов работал бот, запущенный из
исходников (pythonw desktop.py), и новый .exe показывал его интерфейс. Замок
на второй экземпляр тут не помогает — он ловит только другую копию ЭТОГО
приложения, а чужой процесс ему не виден. Порт же один на всех.
"""

import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_free_port_is_not_busy():
    import desktop

    assert desktop.port_busy(_free_port()) is False


def test_occupied_port_is_detected():
    import desktop

    holder = socket.socket()
    holder.bind(('127.0.0.1', 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        assert desktop.port_busy(port) is True, 'занятый порт не распознан'
    finally:
        holder.close()


def test_probe_releases_the_port():
    """
    Проверка не должна сама занимать порт.

    Иначе первый же вызов сделал бы порт занятым для собственного дашборда,
    и приложение отказалось бы запускаться из-за своей же проверки.
    """
    import desktop

    port = _free_port()
    assert desktop.port_busy(port) is False
    assert desktop.port_busy(port) is False       # второй раз — всё ещё свободен

    server = socket.socket()
    server.bind(('127.0.0.1', port))              # значит порт правда свободен
    server.close()


def test_owner_lookup_never_raises():
    """Имя владельца — удобство сообщения, а не условие работы."""
    import desktop

    assert isinstance(desktop.port_owner(_free_port()), str)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
