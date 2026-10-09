"""
Проводка слоёв при запуске бота (реорганизация, этап 10): кто получает
сообщения стратегий (strategies/outbox.py).

Стратегия не импортирует уведомления, тень и журнал отказов — она шлёт в
порт, а здесь, в слое интерфейсов, порт соединяется с модулем. Модуль берётся
в момент вызова: проверки подменяют функции модулей, и подмена должна
срабатывать.
"""

import importlib

PORTS = {'telegram': 'control.telegram_notify', 'shadow': 'shadow', 'refused': 'execution.refused'}


def _receiver(module_name):
    def receive(name, *args, **kwargs):
        return getattr(importlib.import_module(module_name), name)(*args, **kwargs)
    return receive


def install():
    from infra import hooks
    from strategies import outbox
    for port, module_name in PORTS.items():
        outbox.connect(port, _receiver(module_name))
    # Биржа торговли, выбранная на панели, — у счетов; данные спрашивают её
    # через крючок (data/exchange.active_exchange_name).
    hooks.provide('trading_exchange',
                  lambda: importlib.import_module('accounts.settings_store').exchange_name())
