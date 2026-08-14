"""
Освобождение порта после обновления.

ЖАЛОБА, С КОТОРОЙ ВСЁ НАЧАЛОСЬ: «обновляю приложение, оно закрывается и не
запускается, пишет что порт занят, хотя ничего не работает. Приходится руками
заходить в диспетчер задач и снимать процесс».

ПРИЧИНА БЫЛА В ОПОЗНАНИИ. Приложение умеет закрывать свою же прежнюю копию,
но только убедившись, что копия действительно СВОЯ: снимать посторонний
процесс из-за своего запуска нельзя. Проверка шла по имени процесса и
командной строке — и врала в слишком многих случаях:

    запуск из исходников     выглядит как `python.exe -c ...`
    свежие Windows           wmic оттуда удалён, командной строки не добыть
    собранное приложение     поднимает дочерний процесс

Достаточно любого, чтобы своя копия была объявлена посторонней. Дальше
приложение честно отказывалось её трогать и вставало намертво.

РЕШЕНИЕ: спрашивать у самого порта. Отвечает только наше приложение.
"""

import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dashboard  # noqa: E402
import desktop  # noqa: E402

PORT = 8931


def _serve(port):
    dashboard.start_dashboard(port=port)
    for _ in range(40):
        if desktop.port_busy(port):
            return True
        time.sleep(0.2)
    return False


class TestThePortNamesItself:

    def test_our_dashboard_answers_who_it_is(self):
        assert _serve(PORT), 'дашборд не поднялся'
        who = desktop.asks_the_port(PORT)
        assert who and who.get('app') == 'kraken-trade-bot'
        assert who.get('pid') == os.getpid()

    def test_silence_is_an_answer_not_an_error(self):
        """Никто не отвечает — значит не наш. Это ответ, а не сбой."""
        assert desktop.asks_the_port(8999) is None

    def test_our_copy_is_recognised_regardless_of_process_name(self):
        """
        ГЛАВНОЕ СВОЙСТВО. Здесь процесс называется python.exe и в командной
        строке нет ни desktop.py, ни bot.py — ровно тот случай, в котором
        прежняя проверка отвечала «это не наш бот» и требовала диспетчер задач.
        """
        assert _serve(PORT), 'дашборд не поднялся'
        holder = desktop.port_holder(PORT)
        assert desktop.is_our_bot(holder, PORT) is True

    def test_a_stranger_is_still_left_alone(self):
        """
        Чужую программу не трогаем: порт мог занять кто угодно, и снимать
        посторонний процесс из-за нашего запуска нельзя.
        """
        assert desktop.is_our_bot({'name': 'chrome.exe', 'cmd': ''}) is False
        assert desktop.is_our_bot({'name': 'nginx.exe', 'cmd': 'nginx -g'}) is False


class TestFallbackWhenTheAppIsAlreadyDeaf:
    """
    Запасное опознание по имени: приложение может ещё держать сокет, но уже не
    отвечать по сети — тогда спросить некого.
    """

    def test_packaged_app_is_ours(self):
        assert desktop.is_our_bot({'name': 'Kraken.exe', 'cmd': ''}) is True

    def test_python_without_a_command_line_is_ours(self):
        """
        Пустая командная строка — не приговор: на свежих Windows её просто
        нечем добыть, wmic оттуда удалён.
        """
        assert desktop.is_our_bot({'name': 'python.exe', 'cmd': ''}) is True

    def test_python_running_something_else_is_not_ours(self):
        assert desktop.is_our_bot(
            {'name': 'python.exe', 'cmd': 'python manage.py runserver'}) is False


class TestStartupUsesTheProbe:

    def test_startup_passes_the_port_to_the_check(self):
        """
        Без передачи порта опознание падает обратно к именам — то есть к тому,
        что и было сломано.
        """
        text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        assert 'is_our_bot(holder, config.DASHBOARD_PORT)' in text

    def test_command_line_has_a_fallback_without_wmic(self):
        """WMIC удалён из свежих Windows; без запасного пути строка пуста."""
        text = open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8').read()
        assert 'Get-CimInstance Win32_Process' in text
