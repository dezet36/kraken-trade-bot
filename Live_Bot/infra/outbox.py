"""
Сообщения стратегий наверх — без импорта вверх по слоям (реорганизация,
этап 10).

Стратегия говорит, ЧТО случилось (план принят, отказ, тень отказа, обзор
рынка), а куда это записать или отправить — решает верхний слой: получателей
подключает control/wiring.install() при запуске бота (и в проверках). Порты
повторяют имена функций получателей, поэтому вызов в стратегии читается как
прежде:

    from infra.outbox import telegram as tg
    tg.plan_dropped(NAME, pair, side, entry, reason)

    telegram  → telegram_notify (уведомления)
    shadow    → shadow (тень отказа: чем кончился бы сетап)
    refused   → refused (журнал отказов)

Ошибка получателя доходит до стратегии как раньше (у неё вокруг свой
try/except). Получатель не подключён (стенд, разовый скрипт) — вызов молча
ничего не делает: сообщение — не торговля.
"""

_receivers = {}


def connect(port, receiver):
    """receiver(имя_функции, *args, **kwargs) — получатель порта."""
    _receivers[port] = receiver


def disconnect(port=None):
    if port is None:
        _receivers.clear()
    else:
        _receivers.pop(port, None)


def connected(port):
    return port in _receivers


def send(port, name, *args, **kwargs):
    receiver = _receivers.get(port)
    if receiver is None:
        return None
    return receiver(name, *args, **kwargs)


class _Port:
    def __init__(self, port):
        self._port = port

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        return lambda *args, **kwargs: send(self._port, name, *args, **kwargs)


telegram = _Port('telegram')
shadow = _Port('shadow')
refused = _Port('refused')
