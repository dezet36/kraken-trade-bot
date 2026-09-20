"""
Ключи биржи из панели: проверить на бирже и записать в .env.

Раньше это жило в окне первого запуска настольного приложения; окна больше
нет, а вкладка «Подключения» в панели осталась — ей нужны ровно две вещи:
проверка ключей тем же способом, каким потом пойдёт бот, и запись в .env
без потери остальных строк.
"""

import os

import config

ENV_PATH = os.path.join(config.DATA_DIR, '.env')


def write_env(values):
    """
    Дописывает значения в .env, сохраняя всё остальное.

    Значение подставляется в СУЩЕСТВУЮЩУЮ строку, а не дописывается в конец:
    иначе в файле оказались бы два TRADING_MODE, и какой из них подействует —
    вопрос порядка чтения, а не намерения.
    """
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding='utf-8') as fh:
            lines = fh.read().splitlines()

    for key, value in values.items():
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f'{key}=') or stripped.startswith(f'{key} ='):
                lines[i] = f'{key}={value}'
                replaced = True
        if not replaced:
            lines.append(f'{key}={value}')

    with open(ENV_PATH, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines).rstrip() + '\n')


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
