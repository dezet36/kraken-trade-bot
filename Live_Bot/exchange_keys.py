"""
Проверка ключей биржи тем же способом, каким потом пойдёт бот.

С 08.10.2026 ключи хранятся на торговый счёт (accounts/live.py,
secrets/exchange_keys.json в каталоге данных) и вводятся на странице «Счета».
Прежняя запись ключей и режима в общий .env убрана: она переключала весь бот
в демо или бой и останавливала тест стратегий.
"""


def check_keys(exchange, mode, key, secret):
    """
    Пробует ключи на бирже так же, как это потом сделает бот.

    Адрес выбирается по режиму: у демо-счёта он свой, и демо-ключи на боевом
    адресе не работают. Проверка «просто ключи валидные» без учёта режима
    пропустила бы самую частую ошибку — демо-ключи при TRADING_MODE=LIVE.
    """
    import exchange as ex

    endpoint = 'DEMO' if mode in ('DEMO', 'PAPER') else 'LIVE'
    try:
        client = ex.make_client(exchange, key, secret, endpoint)
        client.fetch_balance()
        return True, ''
    except Exception as exc:                       # noqa: BLE001
        return False, str(exc)[:300]
