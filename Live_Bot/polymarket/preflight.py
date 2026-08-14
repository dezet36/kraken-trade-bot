"""
Проверка перед живыми деньгами. Одна команда, которая смотрит всё сразу.

ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА, ЕСЛИ УСЛОВИЯ УЖЕ ЕСТЬ В executor. Там они решают,
отправлять ли ОДНУ заявку, и отвечают в тот момент, когда деньги уже в игре.
Здесь тот же список читается ЗАРАНЕЕ и целиком: что настроено, чего не хватает
и что именно произойдёт, если запустить. Разница как между «двигатель не
завёлся» и «в баке нет бензина» — второе хочется знать до поворота ключа.

ПРОВЕРКА НИЧЕГО НЕ МЕНЯЕТ И НИЧЕГО НЕ ОТПРАВЛЯЕТ. Она только смотрит.

Запуск:
    python -m polymarket.preflight
"""

import io
import sys

from . import book as book_mod
from . import executor, params, store, wallet

OK, WARN, BAD = 'готово', 'внимание', 'НЕТ'


def _line(mark, name, detail=''):
    return {'mark': mark, 'name': name, 'detail': detail}


def check_wallet():
    """Ключ, адрес, клиент и остаток. Ключ не показывается никогда."""
    out = []
    state = wallet.status()
    if not state['configured']:
        out.append(_line(BAD, 'кошелёк не настроен',
                         'нужны PM_PRIVATE_KEY и PM_FUNDER в .env'))
        return out
    out.append(_line(OK, 'кошелёк настроен', f'адрес {state["address"]}'))

    if wallet.client() is None:
        out.append(_line(BAD, 'торговый клиент не поднялся',
                         'ключ есть, но библиотека не приняла его'))
        return out
    out.append(_line(OK, 'торговый клиент поднялся'))

    money = wallet.balance()
    if money is None:
        # НЕИЗВЕСТНО — НЕ НОЛЬ. На нуле торговать нельзя осознанно, при
        # неизвестном нельзя вообще: мы не знаем, чем рискуем.
        out.append(_line(WARN, 'остаток не удалось спросить',
                         'биржа не ответила — повторите позже'))
    else:
        mark = OK if money > 0 else BAD
        out.append(_line(mark, f'остаток ${money:,.2f}',
                         'пусто — торговать нечем' if money <= 0 else ''))
    return out


def check_permission():
    """Разрешение на живой режим и аварийная остановка."""
    out = []
    if wallet.live_enabled():
        out.append(_line(WARN, 'ЖИВОЙ РЕЖИМ ВКЛЮЧЁН',
                         'PM_LIVE=1 — заявки уйдут на биржу'))
    else:
        out.append(_line(OK, 'живой режим выключен',
                         'бумага; для живого нужен PM_LIVE=1'))
    if executor.kill_switch_on():
        out.append(_line(WARN, 'аварийная остановка ВКЛЮЧЕНА',
                         f'файл {executor.KILL_FILE} — заявки не уйдут'))
    else:
        out.append(_line(OK, 'аварийная остановка снята',
                         'создайте файл STOP, чтобы всё прекратилось'))
    return out


def check_budget():
    """Бюджет, потолок заявки и предел дневного убытка."""
    out = []
    money, why = params.budget_plan('MM')
    mark = OK if money > 0 else BAD
    out.append(_line(mark, f'бюджет ${money:,.2f}', why))

    cap = executor.max_order_usd()
    out.append(_line(OK, f'потолок одной заявки ${cap:,.2f}',
                     f'{cap / money:.0%} бюджета' if money else ''))

    stop = money * params.DAILY_LOSS_STOP_PCT / 100
    out.append(_line(OK, f'стоп по дневному убытку ${stop:,.2f}',
                     f'{params.DAILY_LOSS_STOP_PCT:.0f}% бюджета'))
    return out


def check_markets(budget=None):
    """Есть ли на что вставать прямо сейчас."""
    from . import selector
    money = float(budget if budget is not None else params.bankroll_for('MM'))
    if money <= 0:
        return [_line(BAD, 'рынки не проверялись', 'бюджет нулевой')]
    rows = selector.scan(budget=money, limit=40)
    plan = selector.allocate(rows, budget=money)
    markets = plan['markets']
    if not markets:
        return [_line(BAD, 'подходящих рынков нет',
                      f'прошло отбор {len(rows)}, в бюджет не поместился ни один')]
    waits = sorted(m['wait_hours'] for m in markets)
    return [
        _line(OK, f'рынков к работе: {len(markets)}',
              f'вложено ${plan["used"]:,.2f} из ${money:,.2f}'),
        _line(OK, f'ожидание круга: медиана {waits[len(waits) // 2] * 60:.0f} мин',
              f'быстрейший {waits[0] * 60:.0f} мин'),
    ]


def check_data():
    """Пишутся ли журналы, по которым потом судить о результате."""
    import os
    out = []
    for name, path in (('состояние', os.path.join(store.DIR, 'mm_state.json')),
                       ('исполнения', os.path.join(store.DIR, 'mm_fills.jsonl')),
                       ('капитал', os.path.join(store.DIR, 'mm_equity.jsonl')),
                       ('снос цены', os.path.join(store.DIR, 'mm_drift.jsonl')),
                       ('мнение модели', os.path.join(store.DIR, 'mm_shadow.jsonl')),
                       ('живые заявки', executor.ORDERS_LOG)):
        exists = os.path.exists(path)
        size = os.path.getsize(path) if exists else 0
        out.append(_line(OK if exists else WARN, f'журнал «{name}»',
                         f'{size:,} б' if exists else 'ещё не создан'))
    return out


def run(budget=None):
    """Все проверки разом. Возвращает список строк и признак готовности."""
    groups = [
        ('Кошелёк', check_wallet()),
        ('Разрешения', check_permission()),
        ('Деньги', check_budget()),
        ('Рынки', check_markets(budget)),
        ('Журналы', check_data()),
    ]
    bad = sum(1 for _, rows in groups for r in rows if r['mark'] == BAD)
    return groups, bad


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    groups, bad = run()
    print('\nПРОВЕРКА ПЕРЕД ЖИВЫМИ ДЕНЬГАМИ\n')
    for title, rows in groups:
        print(f'  {title}')
        for r in rows:
            tail = f'   — {r["detail"]}' if r['detail'] else ''
            print(f'    [{r["mark"]:>8}] {r["name"]}{tail}')
        print()
    if bad:
        print(f'НЕ ГОТОВО: препятствий {bad}. Живая торговля не начнётся.')
    elif wallet.live_enabled():
        print('ГОТОВО. Живой режим включён — заявки уйдут на биржу.')
    else:
        print('ГОТОВО к бумаге. Для живого режима нужен PM_LIVE=1.')
    return bad


if __name__ == '__main__':
    sys.exit(1 if main() else 0)
