"""
Торговые счета — одна дверь для бота, панели и Telegram (реорганизация,
этап 7).

Какой исполнитель ведёт счёт, решает вид счёта (accounts/live.py): проп «по
инструкциям» — accounts/manual.py, биржа — accounts/onexchange.py. Книги,
решение по сетапу и правила пропа у обоих общие (accounts/books.py). Сбой
одного исполнителя другого не останавливает.
"""

from infra.logger import log

from accounts import books

setup_copy = books.setup_copy
notify_with = books.notify_with
read_journal = books.read_journal


def _engines():
    from accounts import manual, onexchange
    return {'manual': manual, 'exchange': onexchange}


def wants(strategy):
    """Ждёт ли сетапы стратегии хоть один торговый счёт."""
    return any(engine.wants(strategy) for engine in _engines().values())


def offer(strategy, setup):
    """Сетап — всем торговым счетам, выбравшим стратегию:
    [(счёт, None — взят | причина отказа)]."""
    out = []
    for kind, engine in _engines().items():
        try:
            out += engine.offer(strategy, setup)
        except Exception as exc:                   # noqa: BLE001
            log(f'⚠️ торговые счета ({kind}): {strategy} — {exc}')
    return out


def update(market_client):
    """Раз в цикл бота: ведение всех торговых счетов."""
    for kind, engine in _engines().items():
        try:
            engine.update(market_client)
        except Exception as exc:                   # noqa: BLE001
            log(f'⚠️ торговые счета ({kind}): {exc}')


def report(code, rules):
    return _engines()[rules['kind']].report(code, rules)


def act(code, action, item=None, pair=None):
    from accounts import live
    rules = live.get(code)
    if rules is None:
        raise ValueError(f'нет счёта «{code}»')
    return _engines()[rules['kind']].act(code, action, item=item, pair=pair)


def describe():
    out = []
    for engine in _engines().values():
        out += engine.describe()
    return out
