"""
Подключение кошелька из панели: проверка ключа и запись настройки.

ПОЧЕМУ ЭТО ВСЁ-ТАКИ РАЗРЕШЕНО. Долгое время ключ Polymarket принимался только
из окружения, а панель считалась негодной: она слушает без пароля, и секрет,
прошедший через неё, надо считать раскрытым. Довод верный, но применялся он
непоследовательно — ключи БИРЖИ в этом же приложении давно задаются из панели,
проверяются и пишутся в .env. Особый случай для Polymarket ничем не
обосновывался, кроме привычки, и оставлял человека с сервером без способа
подключить кошелёк вообще: консоли там нет.

ЧЕМ ЭТОТ КЛЮЧ ОТЛИЧАЕТСЯ ОТ БИРЖЕВОГО, И ЭТО РАЗЛИЧИЕ НАСТОЯЩЕЕ. Ключ биржи
можно урезать в правах — запретить вывод, оставить только торговлю. Приватный
ключ Polygon урезать нельзя: он распоряжается ВСЕМ, что лежит на адресе, и
подписывает любую операцию, включая перевод всего остатка. Поэтому:

    кошелёк для бота заводится ОТДЕЛЬНЫЙ, и на нём лежит только та сумма,
    которой не жаль в эксперименте.

Это сказано в панели прямым текстом, а не спрятано в документации.

ТРИ ПРАВИЛА, КОТОРЫЕ ЗДЕСЬ СОБЛЮДАЮТСЯ ВСЕГДА:

    1. Ключ НИКОГДА не отдаётся обратно. Наружу уходит только адрес и признак
       «настроен» — по адресу всё видно и ничего нельзя подписать.
    2. Ключ проверяется ДО записи. Записать непроверенный значит узнать о
       негодности в момент первой сделки, а не в момент настройки.
    3. Подключение кошелька НЕ включает живую торговлю. Для неё нужен
       отдельный, явный переключатель — иначе одно действие делало бы два.
"""

import os
import re

from . import params, wallet

KEY_RE = re.compile(r'^(0x)?[0-9a-fA-F]{64}$')
ADDRESS_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')


def mask(value):
    """
    Ключ в виде, годном для показа: начало, конец и длина.

    Полностью скрывать нельзя — человек должен видеть, что записалось именно
    то, что он вводил. Показывать целиком нельзя тем более.
    """
    text = str(value or '')
    if len(text) < 12:
        return '—'
    return f'{text[:6]}…{text[-4:]} ({len(text)} знаков)'


def check(private_key, funder=None):
    """
    Проверяет ключ, НЕ записывая его. Возвращает (годен, адрес, причина).

    Проверка идёт тем же способом, каким потом пойдёт бот: ключ подставляется
    в окружение временно, поднимается настоящий торговый клиент, спрашивается
    адрес. Иначе «проверено» означало бы «похоже на ключ», а это не проверка.
    """
    key = str(private_key or '').strip()
    if not KEY_RE.match(key):
        return False, None, 'ключ должен быть 64 знака в шестнадцатеричном виде'
    if funder and not ADDRESS_RE.match(str(funder).strip()):
        return False, None, 'адрес счёта должен быть вида 0x и 40 знаков'

    saved_key = os.environ.get('PM_PRIVATE_KEY')
    saved_funder = os.environ.get('PM_FUNDER')
    try:
        os.environ['PM_PRIVATE_KEY'] = key
        if funder:
            os.environ['PM_FUNDER'] = str(funder).strip()
        address = wallet.address()
        if not address:
            return False, None, 'ключ не удалось прочитать как приватный'
        client = wallet.client(force=True)
        if client is None:
            return False, address, ('адрес получен, но торговый клиент не '
                                    'поднялся — проверьте адрес счёта')
        return True, address, ''
    except Exception as exc:                                # noqa: BLE001
        # СООБЩЕНИЕ БИБЛИОТЕКИ НАРУЖУ НЕ ИДЁТ. Оно способно содержать сам ключ:
        # у некоторых версий он попадает в текст ошибки целиком.
        return False, None, f'ключ не принят ({type(exc).__name__})'
    finally:
        # Окружение возвращается в прежний вид независимо от исхода: проверка
        # не должна незаметно включить кошелёк, который человек ещё не сохранил.
        for name, value in (('PM_PRIVATE_KEY', saved_key),
                            ('PM_FUNDER', saved_funder)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        wallet.client(force=True)


def save(private_key, funder=None):
    """
    Проверяет и записывает. Возвращает (успех, адрес, сообщение).

    ЖИВАЯ ТОРГОВЛЯ НЕ ВКЛЮЧАЕТСЯ. Подключить кошелёк и разрешить тратить с него
    деньги — два разных решения, и человек принимает их по отдельности.
    """
    ok, address, why = check(private_key, funder)
    if not ok:
        return False, address, why

    import first_run
    values = {'PM_PRIVATE_KEY': str(private_key).strip()}
    if funder:
        values['PM_FUNDER'] = str(funder).strip()
    first_run._write_env(values)

    # Окружение текущего процесса тоже обновляется: иначе настройка
    # подействовала бы только после перезапуска, а человек в панели видел бы
    # «не настроен» сразу после успешного сохранения.
    for name, value in values.items():
        os.environ[name] = value
    wallet.client(force=True)
    return True, address, 'кошелёк подключён; живая торговля пока выключена'


def forget():
    """
    Убирает ключ из настроек и из окружения.

    Нужен ровно затем же, зачем аварийная остановка: способ прекратить всё
    одним действием, не разбираясь, где что записано.
    """
    import first_run
    first_run._write_env({'PM_PRIVATE_KEY': '', 'PM_FUNDER': '', 'PM_LIVE': '0'})
    for name in ('PM_PRIVATE_KEY', 'PM_FUNDER'):
        os.environ.pop(name, None)
    os.environ['PM_LIVE'] = '0'
    wallet.client(force=True)
    return True, 'кошелёк отключён, живая торговля выключена'


def set_live(enabled):
    """
    Включает или выключает живую торговлю. Отдельно от подключения кошелька.

    БЕЗ КОШЕЛЬКА НЕ ВКЛЮЧАЕТСЯ. Разрешение тратить деньги там, где тратить
    нечем, — это не безобидная настройка, а забытый включённым рубильник:
    он сработает в тот момент, когда кошелёк появится.
    """
    import first_run
    if enabled and not wallet.configured():
        return False, 'сначала подключите кошелёк'
    first_run._write_env({'PM_LIVE': '1' if enabled else '0'})
    os.environ['PM_LIVE'] = '1' if enabled else '0'
    return True, ('ЖИВАЯ ТОРГОВЛЯ ВКЛЮЧЕНА — заявки уйдут на биржу'
                  if enabled else 'живая торговля выключена')


def state():
    """
    Что показать в панели. Ключа здесь нет и быть не может.

    Отдаётся адрес, признак настройки, остаток и бюджет — всё, по чему человек
    судит о готовности, и ничего, чем можно подписать сделку.
    """
    status = wallet.status()
    money, why = params.budget_plan('MM')
    out = {
        'configured': status['configured'],
        'address': status['address'],
        'live_enabled': status['live_enabled'],
        'can_trade_live': status['can_trade_live'],
        'budget_usd': round(money, 2),
        'budget_note': why,
        'balance_usd': None,
        'balance_known': False,
    }
    if status['configured']:
        balance = wallet.balance()
        # НЕИЗВЕСТНО И НОЛЬ — РАЗНЫЕ ВЕЩИ, и панель обязана их различать: на
        # нуле торговать нельзя осознанно, при неизвестном нельзя вообще.
        out['balance_known'] = balance is not None
        out['balance_usd'] = round(balance, 2) if balance is not None else None
    return out
