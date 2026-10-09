"""
Крючки между слоями (реорганизация, этап 10): нижний слой спрашивает то, что
знает только верхний, не импортируя его.

Нижний слой зовёт hooks.call('имя', ...), верхний при запуске подставляет
hooks.provide('имя', функция) (control/wiring.py). Крючки живут здесь, а не в
модуле, который их зовёт: проверки перезагружают модули, и подстановка не
должна пропадать вместе с ними.

    trading_exchange  → имя биржи торговли, выбранной на панели (settings_store)
"""

_hooks = {}


def provide(name, func):
    _hooks[name] = func


def call(name, *args, default=None, **kwargs):
    """Значение крючка или default, если его не подставили."""
    func = _hooks.get(name)
    return default if func is None else func(*args, **kwargs)
