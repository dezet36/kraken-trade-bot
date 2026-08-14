"""
Polymarket: доступ к площадке, сбор данных и сигнальные модули.

ПАКЕТ НЕЗАВИСИМ ОТ ТОРГОВЫХ СТРАТЕГИЙ БОТА, и это правило проекта, а не
удобство. FIBO, SMC, LEVELS и RSIBB торгуют бессрочные фьючерсы на бирже; здесь
другая площадка, другие издержки, другой способ разрешения сделки. Общий код
между ними означал бы, что правка ради одного молча меняет другое.

Состав:
    params   — пороги, лимиты, станции; всё через переменные окружения
    client   — Gamma и CLOB, с обходом ловушек, найденных замерами
    store    — накопление снимков цен: истории у площадки нет, копим свою
    weather  — распределение максимума температуры по станции
    longshot — продажа дешёвых лонгшотов; ВАЛИДАЦИЯ ПРОВАЛЕНА, см. модуль
    risk     — размер ставки, лимиты, аварийная остановка
"""

__all__ = ['params', 'client', 'store', 'weather', 'longshot', 'risk']



def _connect_state():
    """Состояние кошелька для панели. Сбой здесь не должен ронять весь снимок."""
    try:
        from . import connect
        return connect.state()
    except Exception:                                       # noqa: BLE001
        return None


def snapshot(limit_markets=20, limit_fills=30):
    """
    Состояние маркет-мейкера для панели. Ключа здесь нет и быть не может.

    Читается ИЗ ФАЙЛОВ, а не из работающего процесса: маркет-мейкер живёт
    отдельно, и панель не должна ни ждать его, ни падать вместе с ним. Данных
    нет — значит он не запускался, и так и сообщаем.
    """
    import json
    import os

    from . import engine, executor, mm, service, store, wallet

    def _tail(path, count):
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-count:]

    state = {}
    if os.path.exists(engine.STATE):
        try:
            with open(engine.STATE, encoding='utf-8') as fh:
                state = json.load(fh)
        except Exception:                                  # noqa: BLE001
            state = {}

    books = state.get('books') or {}
    positions = []
    for token, slot in books.items():
        if not slot.get('position'):
            continue
        positions.append({
            'token': token, 'position': slot['position'],
            'avg_cost': round(slot.get('avg_cost') or 0, 4),
            'realized': round(slot.get('realized') or 0, 2),
            'trades': slot.get('trades') or 0,
        })
    positions.sort(key=lambda r: -abs(r['position']))

    equity = _tail(engine.EQUITY, 200)
    fills = _tail(engine.FILLS, limit_fills)
    orders = _tail(executor.ORDERS_LOG, 40)

    quoting = sum(1 for slot in books.values()
                  if (slot.get('orders') or {}).get('bid')
                  or (slot.get('orders') or {}).get('ask'))

    # СОСТОЯНИЕ БЕРЁТСЯ У ПОТОКА, А НЕ ПО НАЛИЧИЮ ФАЙЛОВ. Файлы остаются от
    # прошлых запусков, и по ним «работает» показывалось бы и после остановки.
    thread = service.status()
    return {
        'running': bool(thread.get('alive')),
        'has_history': bool(equity),
        'service': thread,
        'started': state.get('started'),
        'wallet': wallet.status(),
        # Состояние подключения для панели: адрес, остаток, бюджет. Ключа
        # здесь нет и быть не может — по адресу всё видно и ничего нельзя
        # подписать.
        'connect': _connect_state(),
        'kill_switch': executor.kill_switch_on(),
        'equity': equity[-1] if equity else None,
        'equity_series': [{'at': e.get('at'), 'equity': e.get('equity'),
                           'realized': e.get('realized'),
                           'inventory': e.get('inventory')}
                          for e in equity[-120:]],
        'positions': positions[:limit_markets],
        'positions_total': len(positions),
        'fills': list(reversed(fills)),
        'fills_total': sum(1 for _ in fills),
        'orders_log': list(reversed(orders))[:20],
        'quoting_markets': quoting,
        # Раскладка бюджета: во что вложено и на что рассчитываем. Без неё
        # человек видит «работает» и не знает, чего ждать, а ждать при сотне
        # долларов приходится единиц долларов в месяц — и лучше знать это
        # заранее, чем обнаружить через месяц.
        'plan': {k: v for k, v in (mm.LAST_PLAN or {}).items() if k != 'markets'},
    }
