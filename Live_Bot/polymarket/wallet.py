"""
Кошелёк: ключи, подключение к торговому API и жёсткие предохранители.

КЛЮЧ ЧИТАЕТСЯ ТОЛЬКО ИЗ ОКРУЖЕНИЯ, И ЭТО НЕ ПЕРЕСТРАХОВКА. Приватный ключ
блокчейна — это не пароль, который можно сменить, а прямое распоряжение
средствами: кто его получил, тот и владелец. Отсюда три правила, каждое
проверяется тестом:

    1. Ключ НЕ принимается через панель. Панель слушает без пароля, а всё, что
       через неё прошло, оседает в файле настроек, который сама же и отдаёт по
       /api/settings. Ключ, побывавший там, придётся считать скомпрометированным.
       Ровно это правило уже действует для ключей биржи в этом проекте.
    2. Ключ НЕ попадает в журналы, отчёты и выгрузки. В логи идёт только
       производный адрес — по нему всё видно и ничего нельзя подписать.
    3. Ключ НЕ сохраняется на диск этим кодом. Ни временно, ни «для удобства».

ЖИВАЯ ТОРГОВЛЯ ТРЕБУЕТ ДВУХ НЕЗАВИСИМЫХ УСЛОВИЙ: наличия ключа И явного
разрешения PM_LIVE. Одного ключа мало. Причина простая: ключ появляется в
окружении задолго до того, как человек готов торговать, — например при настройке
или проверке подключения. Если бы этого хватало, первый же запуск начал бы
выставлять заявки на реальные деньги.

ТИП ПОДПИСИ ЗАВИСИТ ОТ ТОГО, КАК СОЗДАН КОШЕЛЁК, и ошибка здесь выглядит как
«заявки отвергаются без причины»:

    0  ключ напрямую (EOA)
    1  кошелёк Polymarket, созданный через email/Magic
    2  кошелёк из браузерного расширения (Metamask и подобные)

У кошелька Polymarket адрес счёта (funder) ОТЛИЧАЕТСЯ от адреса ключа: деньги
лежат на прокси-контракте. Указав вместо него адрес ключа, получим пустой
баланс и отказы.
"""

import os

# Polygon. Сеть зашита намеренно: Polymarket работает только здесь, и
# «настраиваемая сеть» означала бы возможность подписать заявку не туда.
CHAIN_ID = 137
HOST = 'https://clob.polymarket.com'

_client = None


def private_key():
    """Ключ из окружения либо None. Никуда не записывается и не логируется."""
    key = (os.getenv('PM_PRIVATE_KEY') or '').strip()
    return key or None


def funder():
    """Адрес счёта Polymarket (прокси). Для кошелька из расширения — свой же."""
    return (os.getenv('PM_FUNDER') or '').strip() or None


def signature_type():
    """
    Тип подписи. По умолчанию 3 — единственный, который принимает биржа.

    ПРОВЕРЕНО ОТПРАВКОЙ НАСТОЯЩЕЙ ЗАЯВКИ, а не рассуждением. Площадка перешла
    на CTF Exchange v2 и требует подпись смарт-контрактом (POLY_1271, тип 3).
    Типы 0, 1 и 2 проходят проверку подлинности, но на отправке отвечают
    «maker address not allowed, please use the deposit wallet flow» — все три,
    при любом счёте. Именно этим объясняется недоумение в десятке обращений к
    разработчикам площадки: вход работает, торговля нет.

    Старый клиент типа 3 не знал вовсе — там были только 0, 1, 2, — поэтому
    подобрать его перебором было нельзя.
    """
    try:
        return int(os.getenv('PM_SIGNATURE_TYPE', '3'))
    except (TypeError, ValueError):
        return 3


def live_enabled():
    """Явное разрешение торговать деньгами. Без него — только бумага."""
    return (os.getenv('PM_LIVE', '') or '').strip().lower() in ('1', 'true', 'да')


def configured():
    """Есть ли всё для подписи заявок. Разрешение НЕ проверяется — только связка."""
    return bool(private_key() and funder())


def address():
    """
    Адрес, производный от ключа. Безопасен для журналов.

    Возвращает None, если ключа нет или он не разбирается. Ошибку разбора
    наружу не отдаём текстом: сообщение библиотеки может содержать сам ключ.
    """
    key = private_key()
    if not key:
        return None
    try:
        from eth_account import Account
        return Account.from_key(key).address
    except Exception:                                       # noqa: BLE001
        return None


def status():
    """Состояние подключения для панели и журнала. Ключа здесь нет и не будет."""
    return {
        'configured': configured(),
        'live_enabled': live_enabled(),
        'can_trade_live': configured() and live_enabled(),
        'address': address(),
        'funder': funder(),
        'signature_type': signature_type(),
        'chain_id': CHAIN_ID,
    }


_last_failure = {}


def _remember_failure(exc):
    """
    Запоминает, ПОЧЕМУ не поднялся клиент, не выдавая при этом ключа.

    Сообщение библиотеки обезвреживается: если в нём попался ключ или сам
    адрес, они вырезаются. Разряд ошибки при этом сохраняется целиком — по
    нему и различаются случаи, которые снаружи выглядели одинаково.
    """
    text = str(exc)
    key = private_key() or ''
    if key:
        text = text.replace(key, '‹ключ вырезан›')
        if key.startswith('0x'):
            text = text.replace(key[2:], '‹ключ вырезан›')
    _last_failure.clear()
    _last_failure.update({'kind': type(exc).__name__, 'text': text[:220]})


def last_failure():
    """Почему клиент не поднялся в прошлый раз. Пусто — значит поднялся."""
    return dict(_last_failure)


def explain_failure():
    """
    Человеческое объяснение отказа вместо разряда исключения.

    Разбор идёт по узнаваемым признакам: нехватка библиотеки, закрытая сеть,
    отказ площадки. Всё прочее отдаётся как есть — лучше непонятный текст, чем
    молчание.
    """
    fail = last_failure()
    if not fail:
        return ''
    kind, text = fail.get('kind', ''), (fail.get('text') or '')
    low = text.lower()
    if kind in ('ModuleNotFoundError', 'ImportError'):
        return ('в сборку не попала библиотека торгового API — '
                f'обновите приложение ({text})')
    if kind in ('URLError', 'ConnectionError', 'TimeoutError', 'OSError',
                'ConnectionRefusedError', 'socket.timeout'):
        return ('не удалось достучаться до биржи: сеть закрыта, прокси или '
                f'страна под ограничением ({text})')
    if '403' in low or 'forbidden' in low or 'geo' in low or 'blocked' in low:
        return ('биржа отказала в доступе — обычно это ограничение по стране. '
                f'Polymarket закрыт для ряда стран ({text})')
    if '401' in low or 'unauthorized' in low or 'invalid' in low:
        return f'биржа не приняла ключ или подпись ({text})'
    return f'{kind}: {text}'


def client(force=False):
    """
    Клиент торгового API с готовыми учётными данными.

    Возвращает None, если ключа нет: это НЕ ошибка, а обычное состояние
    бумажного режима. Учётные данные API выводятся из ключа подписью, отдельного
    пароля не требуется и никуда не сохраняются.
    """
    global _client
    if _client is not None and not force:
        return _client
    if not configured():
        return None
    try:
        # КЛИЕНТ ВТОРОГО ПОКОЛЕНИЯ, И СТАРЫЙ БОЛЬШЕ НЕ ГОДИТСЯ. Библиотека
        # py-clob-client заархивирована самими разработчиками площадки; на
        # отправку заявки биржа отвечает ей «invalid order version, please use
        # the latest clob-client». Выяснено отправкой, а не чтением
        # документации: чтение остатка и вход работали и на старой.
        #
        # Заодно у нового клиента верный залог. Площадка перешла с USDC.e на
        # собственный pUSD, а старый клиент искал деньги в прежнем токене — и
        # показывал ноль при полном счёте.
        from py_clob_client_v2.client import ClobClient
        made = ClobClient(host=HOST, chain_id=CHAIN_ID, key=private_key(),
                          signature_type=signature_type(), funder=funder())
        # Ключ API ВЫВОДИТСЯ, а не создаётся: создание отвечает отказом, если
        # ключ уже выпущен, а это обычное состояние при каждом перезапуске.
        made.set_api_creds(made.derive_api_key())
        _client = made
        return _client
    except Exception as exc:                                # noqa: BLE001
        # ПРИЧИНА ЗАПОМИНАЕТСЯ, ХОТЯ НАРУЖУ И НЕ ОТДАЁТСЯ ЦЕЛИКОМ. Прежде она
        # пропадала совсем: клиент отвечал «не поднялся», и отличить нехватку
        # библиотеки от закрытой сети или негодного ключа было нечем — человек
        # видел «кошелёк не подключён» и не имел ни единой зацепки.
        #
        # Сам текст библиотеки не показываем: у некоторых версий в него попадает
        # ключ целиком. Сохраняем разряд ошибки и обезвреженное сообщение.
        _remember_failure(exc)
        return None


def balance():
    """
    Свободные средства на счёте Polymarket.

    None — не удалось спросить. Ноль и «неизвестно» здесь разные вещи: на нуле
    торговать нельзя осознанно, при неизвестном — нельзя вообще.
    """
    api = client()
    if api is None:
        return None
    try:
        from py_clob_client_v2.clob_types import (AssetType,
                                                 BalanceAllowanceParams)
        raw = api.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        value = (raw or {}).get('balance')
        # Баланс приходит в минимальных единицах USDC (шесть знаков).
        return float(value) / 1_000_000 if value is not None else None
    except Exception:                                       # noqa: BLE001
        return None
