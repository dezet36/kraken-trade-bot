"""
Отправка заявок на биржу. Всё, что стоит между расчётом и деньгами.

СЕМЬ УСЛОВИЙ ДОЛЖНЫ СОВПАСТЬ, ЧТОБЫ УШЛА ХОТЬ ОДНА ЗАЯВКА. Ни одно из них не
выводится из другого, и это сделано нарочно: любая одиночная ошибка — опечатка
в настройке, забытый флаг, сбой модуля — не приводит к сделке.

    1. явное разрешение PM_LIVE
    2. ключ и адрес счёта в окружении
    3. клиент торгового API поднялся
    4. файл аварийной остановки отсутствует
    5. дневной убыток не достиг предела
    6. размер заявки не выше потолка
    7. цена внутри допустимого диапазона

ПОЧЕМУ АВАРИЙНАЯ ОСТАНОВКА — ФАЙЛ, А НЕ ПЕРЕМЕННАЯ. Переменную нужно менять
там, где запущен процесс, и она действует со следующего перезапуска. Файл можно
создать чем угодно и откуда угодно, и он подействует со следующей заявки —
через считанные секунды. Когда что-то пошло не так, счёт идёт на секунды, а не
на перезапуски.

ЖУРНАЛ ПИШЕТСЯ ДО ОТПРАВКИ, А НЕ ПОСЛЕ. Заявка, ушедшая на биржу и не попавшая
в журнал из-за обрыва, — это позиция, о которой мы не знаем. Обратный порядок
даёт запись без заявки, что безобидно.
"""

import json
import os
import time

from . import params, store, wallet

ORDERS_LOG = os.path.join(store.DIR, 'live_orders.jsonl')
KILL_FILE = os.path.join(store.DIR, 'STOP')

# Потолки живого режима. Они НАМЕРЕННО жёстче бумажных: бумага нужна, чтобы
# увидеть поведение, живой режим — чтобы не потерять деньги, пока поведение
# ещё изучается.
#
# ПОТОЛОК ЗАЯВКИ СЧИТАЕТСЯ ОТ БЮДЖЕТА, А НЕ ЖИВЁТ ОТДЕЛЬНЫМ ЧИСЛОМ. Жёсткие $25
# были больше всего счёта при бюджете в двадцать долларов: заявка, съедающая
# счёт целиком, проходила бы проверку, которая для того и написана. Треть —
# чтобы одно исполнение не могло стать всем экспериментом.
_ORDER_CAP_SHARE = float(os.getenv('PM_MAX_ORDER_SHARE', '0.34'))


def max_order_usd():
    """
    Потолок одной заявки: доля бюджета, но не ниже того, что стоит котировка.

    ПОТОЛОК И РАСКЛАДКА ОБЯЗАНЫ СОГЛАСОВЫВАТЬСЯ, и они разошлись. Раскладка
    отводит рынку минимум биржи — пять контрактов, то есть ровно $5 за
    двустороннюю котировку. Потолок же считал долю бюджета: при $10 это $3.40,
    и продажа на дешёвом рынке (цена 0.20 → встречный токен 0.80, то есть $4)
    отвергалась НАШЕЙ ЖЕ проверкой. Снаружи это опять односторонняя котировка:
    покупка ушла, продажа нет, и ни слова о причине в панели.

    Ни одна сторона не стоит дороже размера: покупка берёт p за контракт,
    продажа (1-p), и обе меньше единицы. Значит потолок в размер котировки
    безопасен по построению и при этом не режет то, что сам же и запланировал.
    """
    hard = os.getenv('PM_MAX_ORDER_USD')
    if hard:
        return float(hard)
    # НАГРАДНЫЙ РАЗМЕР СЮДА НЕ ВПИСЫВАЕТСЯ, И ЭТО ПРАВИЛЬНО.
    #
    # Проверено отправкой: заявка на двадцать контрактов внутри допуска от
    # середины ПОЛУЧАЕТ награду. Вторую сторону той же котировки этот потолок
    # отверг — «$17.00 выше потолка $13.60», — и первым побуждением было его
    # подвинуть. Это была бы ошибка: при бюджете в сорок долларов двадцать
    # контрактов есть половина счёта в одном рынке, а потолок написан ровно
    # против такого.
    #
    # Правильный вывод другой: награда требует не поблажки в проверке, а денег.
    # Доля в треть означает, что наградный размер становится доступен начиная
    # примерно с шестидесяти долларов бюджета — и до тех пор его брать нечем.
    return max(1.0, params.MM_MIN_ORDER_SIZE,
               params.bankroll_for('MM') * _ORDER_CAP_SHARE)


MAX_ORDERS_PER_MINUTE = int(os.getenv('PM_MAX_ORDERS_PER_MINUTE', '60'))

_recent = []


def kill_switch_on():
    """Аварийная остановка. Создайте файл STOP — и заявки прекратятся."""
    return os.path.exists(KILL_FILE)


def engage_kill_switch(reason=''):
    os.makedirs(os.path.dirname(KILL_FILE), exist_ok=True)
    with open(KILL_FILE, 'w', encoding='utf-8') as fh:
        fh.write(f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} {reason}\n')


def release_kill_switch():
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)


def _rate_ok():
    """Не чаще предела: защита и от нашей ошибки, и от лимитов биржи."""
    now = time.time()
    _recent[:] = [t for t in _recent if now - t < 60]
    return len(_recent) < MAX_ORDERS_PER_MINUTE


def can_trade(day_loss_usd=0.0):
    """
    Можно ли отправлять заявки. Возвращает (да/нет, причина).

    Причина возвращается ВСЕГДА, даже при отказе по нескольким условиям сразу:
    молчаливый отказ невозможно отличить от поломки, и разбор превращается в
    гадание.
    """
    if not wallet.live_enabled():
        return False, 'живой режим не включён (PM_LIVE)'
    if not wallet.configured():
        return False, 'нет ключа или адреса счёта в окружении'
    if kill_switch_on():
        return False, 'включена аварийная остановка (файл STOP)'
    limit = float(params.bankroll_for('MM')) * params.DAILY_LOSS_STOP_PCT / 100
    if day_loss_usd >= limit > 0:
        return False, f'дневной убыток {day_loss_usd:.2f} достиг предела {limit:.2f}'
    if wallet.client() is None:
        return False, 'торговый клиент не поднялся'
    if not _rate_ok():
        return False, f'предел {MAX_ORDERS_PER_MINUTE} заявок в минуту'
    return True, ''


def _log(row):
    store._append(ORDERS_LOG, row)


def explain_refusal(exc):
    """
    Отказ биржи по-человечески. Непонятный текст — тоже ответ, но плохой.

    ПОЧЕМУ ЭТО ОТДЕЛЬНАЯ ФУНКЦИЯ. Одна строка от биржи стоила целого разбора:
    «the order signer address has to be the address of the API KEY» означает
    ровно одно — в поле «счёт Polymarket» указан адрес КОШЕЛЬКА, а нужен адрес
    СЧЁТА на Polymarket. Это разные адреса, и человек, который завёл кошелёк
    через расширение, уверен, что адрес у него один.

    Проверено обеими заявками на живом счёте:
        счёт = адрес кошелька Phantom   ОТКАЗ этой самой строкой
        счёт = адрес Polymarket         ПРИНЯТА
    """
    text = str(exc)
    low = text.lower()
    if 'signer address' in low and 'api key' in low:
        return ('в поле «счёт Polymarket» указан адрес вашего КОШЕЛЬКА, а '
                'нужен адрес СЧЁТА на Polymarket — это разные адреса. '
                'Посмотрите его на polymarket.com в профиле (Deposit / '
                'адрес счёта) и впишите сюда')
    if 'not enough balance' in low or 'balance is not enough' in low:
        return ('на счёте Polymarket не хватает денег под эту заявку — '
                'проверьте остаток и бюджет стратегии')
    if 'invalid order version' in low or 'clob-client' in low:
        return ('биржа не приняла формат заявки — приложению нужна свежая '
                'версия торговой библиотеки, обновитесь')
    if 'minimum' in low and ('size' in low or 'amount' in low):
        return 'размер заявки ниже минимума этого рынка'
    if 'tick' in low or 'price' in low and 'increment' in low:
        return 'цена не попадает в шаг цен этого рынка'
    return f'биржа отвергла: {text[:120]}'


def route(side, price, size, holding=0.0, twin_token=None, token_id=None,
          tick=0.001):
    """
    Каким токеном исполнять сторону котировки. Возвращает (токен, сторона, цена).

    ЗДЕСЬ ЖИВЁТ ГЛАВНАЯ ПОЛОМКА ВСЕЙ ЗАТЕИ, И ОНА БЫЛА НЕ В КОДЕ, А В ЕГО
    ОТСУТСТВИИ. Биржа не даёт продать токен, которого у нас нет. Проверено
    отправкой настоящей заявки на живом счёте:

        покупка «ДА»  5 по 0.066    принята
        ПРОДАЖА «ДА»  5 по 0.98     ОТКАЗ: balance 0, order amount 5000000
        покупка «НЕТ» 5 по 0.334    принята

    То есть КАЖДАЯ наша продажа отвергалась биржей, и на Polymarket стояли
    только покупки. Бот не был маркет-мейкером вовсе — он был односторонним
    покупателем, ровно тем, чей разобранный кошелёк держит переоценку -$8 564
    и ради ухода от которого всё и затевалось.

    ЧИНИТСЯ ЭТО БЕЗ ХИТРОСТЕЙ. У бинарного рынка два токена, и продажа «ДА» по
    цене A есть покупка «НЕТ» по цене (1-A): один погасится единицей, другой
    нулём. Обе стороны становятся покупками, обе биржа принимает, а стоимость
    двусторонней котировки остаётся ровно размером — как и считал quote_cost
    всё это время. Расчёт предполагал этот путь; в отправке заявок его не было.

    Когда токен У НАС ЕСТЬ, продаём по-настоящему: это дешевле (не требует
    новых денег) и сразу закрывает круг.
    """
    price = float(price)
    if side == 'bid':
        # Держим «НЕТ» — продать его выгоднее, чем покупать «ДА»: закрывает
        # пару и высвобождает деньги вместо того, чтобы занимать новые.
        if twin_token and holding <= -float(size):
            return {'token': str(twin_token), 'side': 'SELL',
                    'price': _mirror(price, tick), 'mirrored': True,
                    'why': 'продаём встречный токен — он у нас есть'}
        return {'token': str(token_id), 'side': 'BUY', 'price': price,
                'mirrored': False, 'why': 'обычная покупка'}

    if holding >= float(size):
        return {'token': str(token_id), 'side': 'SELL', 'price': price,
                'mirrored': False, 'why': 'продаём то, что держим'}
    if twin_token:
        return {'token': str(twin_token), 'side': 'BUY',
                'price': _mirror(price, tick), 'mirrored': True,
                'why': 'продажа через покупку встречного токена'}
    return {'token': None, 'side': None, 'price': None, 'mirrored': False,
            'why': 'нечего продавать и нет встречного токена'}


def _mirror(price, tick):
    """Цена встречного токена: (1 - p), прижатая к сетке биржи."""
    step = float(tick or 0.001)
    mirrored = 1.0 - float(price)
    return round(round(mirrored / step) * step, 10)


def place(token_id, side, price, size, day_loss_usd=0.0, tick=0.001,
          twin_token=None, holding=0.0):
    """
    Выставляет лимитную заявку. Возвращает результат словарём.

    ТОЛЬКО ЛИМИТНЫЕ И ТОЛЬКО GTC. Рыночная заявка сделала бы нас тейкером — то
    есть заплатила бы комиссию, ради отсутствия которой всё и затевалось: на
    цене 0.05 тейкер отдаёт 4.75% ставки, мейкер ноль.

    twin_token — встречный токен того же рынка. Без него продать можно только
    то, что уже держим; см. route.
    """
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    plan = route(side, price, size, holding=holding, twin_token=twin_token,
                 token_id=token_id, tick=tick)
    if not plan['token']:
        _log({'at': stamp, 'action': 'REFUSE', 'why': plan['why'],
              'token': token_id, 'side': side, 'price': price, 'size': size})
        return {'ok': False, 'why': plan['why']}

    send_token, send_side, send_price = plan['token'], plan['side'], plan['price']
    notional = float(send_price) * float(size)

    allowed, why = can_trade(day_loss_usd)
    if not allowed:
        _log({'at': stamp, 'action': 'REFUSE', 'why': why, 'token': token_id,
              'side': side, 'price': price, 'size': size})
        return {'ok': False, 'why': why}

    cap = max_order_usd()
    if notional > cap:
        why = f'размер ${notional:.2f} выше потолка ${cap:.2f}'
        _log({'at': stamp, 'action': 'REFUSE', 'why': why, 'token': token_id,
              'side': side, 'price': price, 'size': size})
        return {'ok': False, 'why': why}

    if not (tick <= send_price <= 1 - tick):
        why = f'цена {send_price} вне диапазона'
        _log({'at': stamp, 'action': 'REFUSE', 'why': why, 'token': token_id,
              'side': side, 'price': price, 'size': size})
        return {'ok': False, 'why': why}

    # ЗАПИСЬ ДО ОТПРАВКИ. Заявка, ушедшая на биржу и не попавшая в журнал из-за
    # обрыва, — это позиция, о которой мы не знаем.
    _log({'at': stamp, 'action': 'SEND', 'token': send_token, 'side': side,
          'price': send_price, 'size': size, 'notional': round(notional, 2),
          'mirrored': plan['mirrored'], 'asked_price': price,
          'route': plan['why']})
    _recent.append(time.time())

    try:
        # КЛИЕНТ ВТОРОГО ПОКОЛЕНИЯ. Старая библиотека заархивирована, и биржа
        # отвечает ей «invalid order version» — выяснено отправкой настоящей
        # заявки, а не чтением документации.
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
        api = wallet.client()
        signed = api.create_order(OrderArgsV2(
            token_id=str(send_token), price=float(send_price),
            size=float(size), side=send_side))
        answer = api.post_order(signed, OrderType.GTC)
    except Exception as exc:                                # noqa: BLE001
        _log({'at': stamp, 'action': 'ERROR', 'token': send_token,
              'why': str(exc)[:200]})
        return {'ok': False, 'why': explain_refusal(exc)}

    order_id = (answer or {}).get('orderID') or (answer or {}).get('orderId')
    _log({'at': stamp, 'action': 'PLACED', 'token': send_token, 'side': side,
          'price': send_price, 'size': size, 'order_id': order_id,
          'mirrored': plan['mirrored'], 'answer': str(answer)[:200]})
    return {'ok': True, 'order_id': order_id, 'answer': answer,
            'route': plan}


def cancel(order_id):
    """Снимает одну заявку."""
    api = wallet.client()
    if api is None:
        return {'ok': False, 'why': 'клиента нет'}
    try:
        # Снятие принимает СВОЙ тип, а не голую строку: в новом клиенте
        # cancel_order ждёт OrderPayload, и передача строки молча падает.
        from py_clob_client_v2.clob_types import OrderPayload
        answer = api.cancel_order(OrderPayload(orderID=str(order_id)))
    except Exception as exc:                                # noqa: BLE001
        return {'ok': False, 'why': str(exc)[:120]}
    _log({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
          'action': 'CANCEL', 'order_id': order_id})
    return {'ok': True, 'answer': answer}


def cancel_all():
    """
    Снимает ВСЕ заявки. Вызывается при остановке и при аварии.

    Оставленные без присмотра заявки — худшее из возможных состояний: мы не
    котируем, но нас продолжают исполнять, причём именно тогда, когда это
    выгодно встречной стороне.
    """
    api = wallet.client()
    if api is None:
        return {'ok': False, 'why': 'клиента нет'}
    try:
        answer = api.cancel_all()
    except Exception as exc:                                # noqa: BLE001
        return {'ok': False, 'why': str(exc)[:120]}
    _log({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
          'action': 'CANCEL_ALL', 'answer': str(answer)[:200]})
    return {'ok': True, 'answer': answer}


def open_orders():
    """
    Наши заявки, как их видит биржа. Источник правды при расхождении.

    МЕТОД НАЗЫВАЕТСЯ get_open_orders, И ЭТО БЫЛА ТИХАЯ ПОЛОМКА. У клиента
    второго поколения такого имени, как раньше, нет вовсе; вызов падал с
    «object has no attribute get_orders», исключение глоталось, и функция
    честно возвращала None. Следствие серьёзнее опечатки: сверка с биржей
    (reconcile) получала None и молча пропускалась КАЖДЫЙ такт. Бот ни разу не
    проверил, существуют ли его заявки на самом деле, — а панель показывала
    «стоим на трёх рынках» из бумажной модели. Человек смотрел на Polymarket,
    не видел там ничего и не понимал, кто из двоих врёт.
    """
    api = wallet.client()
    if api is None:
        return None
    try:
        return api.get_open_orders()
    except Exception as exc:                                # noqa: BLE001
        _log({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'action': 'ASK_FAILED', 'why': str(exc)[:200]})
        return None


def scoring(order_ids):
    """
    Попадают ли заявки под награду ПО МНЕНИЮ БИРЖИ.

    Наша собственная проверка (book.rewards_eligible) считает по описанным
    правилам, но правила меняются, а биржа отвечает по факту. Расхождение между
    ними — сигнал, что наша модель отстала.
    """
    api = wallet.client()
    if api is None or not order_ids:
        return None
    try:
        from py_clob_client_v2.clob_types import OrdersScoringParams
        return api.are_orders_scoring(OrdersScoringParams(orderIds=list(order_ids)))
    except Exception:                                       # noqa: BLE001
        return None


def own_trades(after_ts=None):
    """
    НАШИ сделки по мнению биржи. Источник правды в живом режиме.

    ПОЧЕМУ НЕ ЛЕНТА. В бумаге исполнение определяется по общей ленте и модели
    очереди — иначе никак. В живом режиме такая оценка становится не только
    лишней, но и вредной: она отвечает «исполнилось бы», тогда как биржа знает
    «исполнилось». Разойдутся они обязательно (очередь оценивается
    приблизительно), и учёт поехал бы, а вслед за ним — размер позиции.

    Возвращает None, если спросить не удалось. Пустой список и «не спросили» —
    разные вещи: на первом можно строить решение, на втором нельзя.
    """
    api = wallet.client()
    if api is None:
        return None
    try:
        from py_clob_client_v2.clob_types import TradeParams
        params_obj = TradeParams(after=int(after_ts)) if after_ts else None
        rows = api.get_trades(params_obj)
    except Exception:                                       # noqa: BLE001
        return None
    return our_part(rows, wallet.funder())


def our_part(rows, funder):
    """
    Наша доля в сделках биржи. Верхний уровень записи — НЕ наша сделка.

    САМАЯ ДОРОГАЯ ОШИБКА ВСЕГО ПРОЕКТА, и обнаружилась она по расхождению:
    панель показывала +$290 прибыли, тогда как на счёте было минус два доллара.

    Запись о сделке описывает ВЕСЬ мэтч — то есть заявку тейкера целиком и всех
    мейкеров, которых он собрал. Наши пять контрактов лежат внутри, в
    maker_orders, вместе с НАШЕЙ ценой, НАШИМ токеном и НАШЕЙ стороной. Верхние
    поля принадлежат тейкеру и к нам отношения не имеют:

        верхний уровень   size 1070.54  price 0.401  токен «Нет»
        наша строка       matched_amount 5  price 0.637  токен «Да»

    Записывая верхний уровень, бот приписывал себе тысячу контрактов вместо
    пяти, чужую цену и ЧУЖОЙ ТОКЕН. Отсюда позиции в тысячи контрактов при
    бюджете в сорок долларов, деньги, ушедшие в минус тысячу восемьсот, и
    прибыль, которой никогда не было.

    Одна запись может содержать НЕСКОЛЬКО наших заявок — тейкер способен снять
    сразу два наших уровня. Поэтому на каждую свою строку выдаётся отдельное
    исполнение, а ключ для защиты от повторов включает номер заявки: по одному
    лишь номеру сделки второе исполнение потерялось бы молча.
    """
    mine = (funder or '').lower()
    out = []
    for row in rows or []:
        try:
            trade_id = str(row.get('id') or '')
            stamp = int(row.get('match_time') or row.get('timestamp') or 0)
            status = row.get('status')

            # Мы тейкер — тогда верхний уровень наш, и он единственный.
            taker = str(row.get('maker_address') or '').lower()
            parts = []
            if mine and taker == mine:
                parts.append({
                    'key': trade_id,
                    'token': str(row.get('asset_id') or ''),
                    'side': str(row.get('side', '')),
                    'price': row.get('price'),
                    'size': row.get('size'),
                    'order_id': row.get('taker_order_id'),
                })
            for part in row.get('maker_orders') or []:
                if mine and str(part.get('maker_address') or '').lower() != mine:
                    continue
                parts.append({
                    'key': f"{trade_id}:{part.get('order_id')}",
                    'token': str(part.get('asset_id') or ''),
                    'side': str(part.get('side', '')),
                    'price': part.get('price'),
                    'size': part.get('matched_amount'),
                    'order_id': part.get('order_id'),
                })

            for part in parts:
                out.append({
                    'id': part['key'],
                    'token': part['token'],
                    'side': 'bid' if part['side'].upper() == 'BUY' else 'ask',
                    'price': float(part['price']),
                    'size': float(part['size']),
                    'ts': stamp,
                    'order_id': part['order_id'],
                    'status': status,
                })
        except (TypeError, ValueError):
            continue
    return out


def reconcile(expected_orders):
    """
    Сверка нашего представления с биржей.

    expected_orders — {биржевой номер: (токен, сторона, цена)}. Возвращает
    расхождения обеих сторон:

        'ghost'   заявка есть у нас, но её нет на бирже. Опаснее второго: мы
                  считаем, что котируем, а на деле нет — и не котируем ту
                  сторону, которой рассчитывали сокращать запас.
        'orphan'  заявка есть на бирже, но не у нас. Это неснятая старая: нас
                  исполнят по цене, которую мы уже забыли.

    None — сверка не состоялась. Отсутствие расхождений и невозможность
    спросить биржу — разные вещи, и путать их нельзя.
    """
    rows = open_orders()
    if rows is None:
        return None
    live_ids = set()
    for row in rows or []:
        oid = row.get('id') or row.get('orderID')
        if oid:
            live_ids.add(str(oid))
    ours = {str(k) for k in (expected_orders or {})}
    return {'ghost': sorted(ours - live_ids),
            'orphan': sorted(live_ids - ours),
            'live_count': len(live_ids), 'our_count': len(ours)}
