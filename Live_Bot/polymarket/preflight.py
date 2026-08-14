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


def check_reach():
    """
    Доходит ли вообще до биржи. Проверяется ДО ключа и отдельно от него.

    ЗАЧЕМ ОТДЕЛЬНО. Закрытая сеть, прокси и ограничение по стране выглядели
    ровно как негодный ключ: «кошелёк не подключён» и ни слова о причине.
    Человек проверял ключ там, где дело было в сети, и проверял правильно —
    ключ был в порядке.

    Библиотека тоже проверяется здесь: в собранном приложении её может не
    оказаться, и это отдельная беда, которую нельзя путать с остальными.
    """
    import urllib.request
    out = []
    try:
        import py_clob_client_v2  # noqa: F401
        out.append(_line(OK, 'библиотека торгового API на месте'))
    except Exception as exc:                                # noqa: BLE001
        out.append(_line(BAD, 'библиотеки торгового API нет',
                         f'{type(exc).__name__}: обновите приложение'))
        return out
    try:
        request = urllib.request.Request(
            'https://clob.polymarket.com/', headers={'User-Agent': 'kraken-bot'})
        with urllib.request.urlopen(request, timeout=12) as answer:
            out.append(_line(OK, 'биржа отвечает', f'код {answer.status}'))
    except Exception as exc:                                # noqa: BLE001
        code = getattr(exc, 'code', None)
        if code in (403, 451):
            out.append(_line(BAD, f'биржа отказала в доступе (код {code})',
                             'обычно это ограничение по стране'))
        else:
            out.append(_line(BAD, 'биржа не отвечает',
                             f'{type(exc).__name__}: сеть, прокси или брандмауэр'))
    return out


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
                         wallet.explain_failure()
                         or 'причина неизвестна'))
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
        detail = ''
        if money <= 0:
            # ПУСТО НА КАКОМ ИМЕННО АДРЕСЕ — вот чего не хватало. Строчка
            # «остаток $0.00, торговать нечем» верна и бесполезна: деньги могут
            # лежать на счёте площадки, а искали мы их на адресе ключа.
            #
            # Различить эти случаи легко, и различие решает всё: у кошелька,
            # заведённого через сайт, счёт ОТДЕЛЬНЫЙ, и вычислить его нельзя —
            # у новых счетов своё устройство, известные фабрики адрес не дают.
            # Значит человек должен взять его на polymarket.com/settings.
            # ЗАСТРЯВШИЙ ТИП ПОДПИСИ — первое, что надо проверить. Он
            # записывается в настройки при подключении, и однажды записанный
            # переживает обновления: прежняя версия подбирала из 0, 1, 2, а
            # верный оказался третий. Замер на живом счёте: типы 0-2 дают
            # $0.00, тип 3 — настоящий остаток.
            if wallet.signature_type() != 3:
                out.append(_line(
                    WARN, f'тип подписи {wallet.signature_type()} — не тот',
                    'биржа перешла на тип 3; нажмите «Подключить» ещё раз, '
                    'чтобы он подобрался заново'))
            where = state.get('funder') or ''
            same = where.lower() == (state.get('address') or '').lower()
            detail = ('деньги искались на адресе КЛЮЧА. Если счёт Polymarket '
                      'другой — возьмите его на polymarket.com/settings и '
                      'впишите в поле «Адрес счёта»'
                      if same else
                      f'на счёте {where[:10]}…{where[-4:]} пусто — пополните '
                      f'его на polymarket.com')
        out.append(_line(mark, f'остаток ${money:,.2f}', detail))
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
        ('Связь', check_reach()),
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
