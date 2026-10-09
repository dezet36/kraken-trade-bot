"""
Проводка слоёв при запуске бота (реорганизация, этап 10): кто получает
сообщения стратегий (infra/outbox.py).

Стратегия не импортирует уведомления, тень и журнал отказов — она шлёт в
порт, а здесь, в слое интерфейсов, порт соединяется с модулем. Модуль берётся
в момент вызова: проверки подменяют функции модулей, и подмена должна
срабатывать.
"""

import importlib

PORTS = {'telegram': 'control.telegram_notify', 'shadow': 'execution.shadow', 'refused': 'execution.refused'}


def _receiver(module_name):
    def receive(name, *args, **kwargs):
        return getattr(importlib.import_module(module_name), name)(*args, **kwargs)
    return receive


def install():
    from infra import hooks
    from infra import outbox
    for port, module_name in PORTS.items():
        outbox.connect(port, _receiver(module_name))
    # Биржа торговли, выбранная на панели, — у счетов; данные спрашивают её
    # через крючок (data/exchange.active_exchange_name).
    hooks.provide('trading_exchange',
                  lambda: importlib.import_module('accounts.settings_store').exchange_name())
    # Пределы портфеля — правила счёта; брокер и боевой исполнитель спрашивают
    # их через крючок (execution/risk_gate.portfolio_limits).
    hooks.provide('portfolio_limits', _portfolio_limits)


def _portfolio_limits():
    store = importlib.import_module('accounts.settings_store')
    return (store.portfolio_max_positions(), store.portfolio_risk_pct(),
            store.daily_loss_pct())
