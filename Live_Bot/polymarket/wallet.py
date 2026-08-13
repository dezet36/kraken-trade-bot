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
    try:
        return int(os.getenv('PM_SIGNATURE_TYPE', '1'))
    except (TypeError, ValueError):
        return 1


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
        from py_clob_client.client import ClobClient
        made = ClobClient(host=HOST, chain_id=CHAIN_ID, key=private_key(),
                          signature_type=signature_type(), funder=funder())
        made.set_api_creds(made.create_or_derive_api_creds())
        _client = made
        return _client
    except Exception:                                       # noqa: BLE001
        # Текст ошибки наружу не отдаём: он может содержать ключ целиком.
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
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        raw = api.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        value = (raw or {}).get('balance')
        # Баланс приходит в минимальных единицах USDC (шесть знаков).
        return float(value) / 1_000_000 if value is not None else None
    except Exception:                                       # noqa: BLE001
        return None
