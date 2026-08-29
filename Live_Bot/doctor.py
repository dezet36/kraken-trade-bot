"""
Проверка готовности к запуску: что не так и как это чинить.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Бот, запущенный с неверным ключом или в каталоге без
прав на запись, падает где-то в середине первого цикла — со стеком, из
которого причина не видна. На сервере, куда только что скопировали папку,
это самый частый способ потерять полчаса. Здесь всё, что может пойти не так
ДО первой сделки, проверяется по порядку и объясняется по-русски.

Каждая проверка возвращает уровень:

    ok     всё в порядке
    warn   работать будет, но не так, как вы, вероятно, ожидаете
    fail   торговать нельзя, запуск бессмысленен

Запуск отдельно:
    python Live_Bot/doctor.py
"""

import os
import sys

OK, WARN, FAIL = 'ok', 'warn', 'fail'

MIN_PYTHON = (3, 10)
REQUIRED_PACKAGES = (
    ('ccxt', 'биржевое API'),
    ('pandas', 'работа со свечами'),
    ('numpy', 'расчёты'),
    ('dotenv', 'чтение .env'),
    ('requests', 'сеть'),
)


def _result(level, title, detail='', fix=''):
    return {'level': level, 'title': title, 'detail': detail, 'fix': fix}


def check_python():
    v = sys.version_info
    text = f'{v.major}.{v.minor}.{v.micro}'
    if (v.major, v.minor) < MIN_PYTHON:
        return _result(FAIL, f'Python {text} слишком старый',
                       f'нужен {MIN_PYTHON[0]}.{MIN_PYTHON[1]} или новее',
                       'установите свежий Python и пересоздайте окружение')
    return _result(OK, f'Python {text}')


def check_packages():
    missing = []
    for module, purpose in REQUIRED_PACKAGES:
        try:
            __import__(module)
        except ImportError:
            missing.append(f'{module} ({purpose})')
    if missing:
        return _result(FAIL, 'Не установлены зависимости',
                       ', '.join(missing),
                       'pip install -r requirements.txt')
    return _result(OK, 'Зависимости на месте')


def check_data_dir():
    import config
    path = config.DATA_DIR
    if not os.path.isdir(path):
        return _result(FAIL, 'Каталог данных не создан', path,
                       'создайте каталог или задайте BOT_DATA_DIR')
    probe = os.path.join(path, '.write_test')
    try:
        with open(probe, 'w', encoding='utf-8') as fh:
            fh.write('x')
        os.remove(probe)
    except Exception as exc:                       # noqa: BLE001
        return _result(FAIL, 'В каталог данных нельзя писать',
                       f'{path}: {exc}',
                       'дайте права на запись пользователю, от которого работает бот')

    # Данные ВНУТРИ кода переживут запуск, но не переживут обновление
    # копированием папки поверх старой.
    code_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.abspath(path) == os.path.abspath(code_dir):
        return _result(WARN, 'Данные лежат внутри папки с кодом', path,
                       'задайте BOT_DATA_DIR на каталог вне Live_Bot, иначе '
                       'копирование новой версии поверх старой сотрёт журнал '
                       'сделок и состояние позиций')
    return _result(OK, 'Каталог данных доступен на запись', path)


def check_env():
    import config
    if not os.path.exists(os.path.join(config.DATA_DIR, '.env')) and not config.env_loaded:
        return _result(FAIL, 'Файл .env не найден',
                       f'ожидался в {config.DATA_DIR}',
                       'скопируйте .env.example в .env и впишите ключи')
    return _result(OK, 'Файл .env прочитан')


def check_keys():
    import config
    name = (config.EXCHANGE_NAME or '').lower()
    if name == 'bybit':
        key, secret = config.BYBIT_API_KEY, config.BYBIT_SECRET_KEY
    elif name == 'bingx':
        key, secret = config.BINGX_API_KEY, config.BINGX_SECRET_KEY
    else:
        return _result(FAIL, f'Неизвестная биржа: {config.EXCHANGE_NAME}', '',
                       'EXCHANGE=bybit или bingx')
    if not key or not secret:
        if config.PAPER_MODE:
            return _result(WARN, f'Нет ключей {name}',
                           'в фантомном режиме сделки не отправляются, но '
                           'котировки берутся с биржи',
                           'впишите ключи в .env — даже для фантома нужны данные')
        return _result(FAIL, f'Нет ключей {name}', '',
                       'впишите API-ключ и секрет в .env')
    return _result(OK, f'Ключи {name} загружены')


def check_mode():
    import config
    mode = config.TRADING_MODE
    if mode == 'LIVE':
        return _result(WARN, 'Режим LIVE — торговля реальными деньгами',
                       f'риск на сделку {config.RISK_PER_TRADE}%',
                       'для проверки поставьте TRADING_MODE=PAPER')
    if mode == 'PAPER':
        return _result(OK, 'Режим PAPER — ордера на биржу не отправляются')
    return _result(OK, f'Режим {mode}')


def check_strategy():
    import config
    known = ('FIBO', 'SMC', 'LEVELS', 'BOTH')
    if config.STRATEGY not in known:
        return _result(FAIL, f'Неизвестная стратегия: {config.STRATEGY}',
                       '', f'STRATEGY = один из {", ".join(known)}')
    return _result(OK, f'Стратегия: {config.STRATEGY}')


def check_exchange():
    """Живая проверка связи: ключи могут быть на месте и при этом неверны."""
    import config
    # Проверяем ровно тот вызов, которым живёт бот: свечи. Тикер идёт по
    # другому маршруту и может падать там, где торговля работает.
    try:
        import exchange
        ex = exchange.get_exchange()
        candles = ex.fetch_ohlcv(config.TRADING_PAIRS_POOL[0], '1h', limit=5)
        if not candles:
            raise RuntimeError('биржа вернула пустой список свечей')
    except Exception as exc:                       # noqa: BLE001
        return _result(FAIL, 'Биржа недоступна', str(exc)[:200],
                       'проверьте интернет, ключи и права ключа '
                       '(нужна торговля фьючерсами)')
    if config.PAPER_MODE:
        return _result(OK, 'Связь с биржей есть (котировки)')
    try:
        ex.fetch_balance()
    except Exception as exc:                       # noqa: BLE001
        return _result(FAIL, 'Ключи не дают доступа к счёту', str(exc)[:200],
                       'проверьте права ключа и IP-фильтр в кабинете биржи')
    return _result(OK, 'Связь с биржей и доступ к счёту есть')


def check_pairs():
    import config
    pool = config.TRADING_PAIRS_POOL
    if not pool:
        return _result(FAIL, 'Пул пар пуст', '', 'задайте TRADING_PAIRS_POOL')
    return _result(OK, f'Пул: {len(pool)} пар')


def _strategy_risks():
    """
    Риск на сделку и число слотов КАЖДОЙ стратегии.

    Отказ чтения одной не должен скрывать остальные: стратегия, чей модуль
    параметров не загрузился, — сама по себе повод для отчёта, а не причина
    промолчать обо всём.
    """
    import config
    import settings_store as settings
    out = []
    sources = (
        ('FIBO', lambda: config.RISK_PER_TRADE),
        ('SMC', lambda: __import__('smc.params', fromlist=['x']).RISK_PER_TRADE_PCT),
        ('LEVELS', lambda: __import__('levels.params', fromlist=['x']).RISK_PCT),
        ('RSIBB', lambda: __import__('rsibb.params', fromlist=['x']).RISK_PCT),
    )
    for name, get in sources:
        try:
            risk = float(get())
        except Exception:                          # noqa: BLE001
            out.append((name, None, None))
            continue
        try:
            slots = int(settings.max_slots(name))
        except Exception:                          # noqa: BLE001
            slots = 0
        out.append((name, slots, risk))
    return out


def check_risk():
    """
    Риск по ВСЕМ стратегиям и то, какие пределы сейчас выключены.

    Здесь читался только config.RISK_PER_TRADE — параметр FIBO — и по нему
    выносился вердикт обо всём боте. SMC при этом рискует вдвое больше, и
    диагностика этого не видела: показывала «риск 0.5%, до 2.5% суммарно»,
    когда одновременно под риском могло стоять больше сорока процентов.
    """
    import risk_gate
    rows = _strategy_risks()

    broken = [n for n, _s, r in rows if r is None]
    if broken:
        return _result(WARN, f'Параметры не прочитаны: {", ".join(broken)}',
                       'риск этих стратегий неизвестен',
                       'проверьте значения в .env — числа пишутся через точку')

    total, unbounded = risk_gate.max_exposure(
        [(n, s, r) for n, s, r in rows])
    spread = ', '.join(f'{n} {r:g}%' for n, _s, r in rows)

    if unbounded:
        return _result(WARN,
                       f'Без предела позиций: {", ".join(unbounded)}',
                       f'остальные дают до {total:.1f}% депозита под риском '
                       f'одновременно ({spread})',
                       'задайте число слотов этим стратегиям')
    if total > 20:
        return _result(WARN, f'Одновременно под риском до {total:.1f}% депозита',
                       f'{spread}',
                       'уменьшите слоты, риск на сделку или включите предел '
                       'портфеля')
    return _result(OK, f'Под риском одновременно до {total:.1f}% депозита',
                   spread)


def check_limits():
    """
    Какие предохранители сейчас выключены.

    Выключенный предел выглядит настроенным: в поле стоит ноль, и на глаз это
    неотличимо от «ещё не задал». Пока об этом никто не говорил, оба
    портфельных предела стояли выключенными, а панель показывала их значения
    так, будто они работают.
    """
    import risk_gate
    import settings_store as settings
    try:
        import config
        off = risk_gate.disabled_limits(settings.portfolio_max_positions(),
                                        settings.portfolio_risk_pct(),
                                        settings.daily_loss_pct(),
                                        config.MAX_ENTRY_COST_SHARE_PCT)
    except Exception as exc:                       # noqa: BLE001
        return _result(WARN, 'Пределы портфеля не прочитаны', str(exc),
                       'проверьте настройки в панели')
    if not off:
        return _result(OK, 'Предохранители включены',
                       'предел позиций, предел риска портфеля, дневной предел '
                       'и предел расхода на вход')
    return _result(WARN, f'Выключено предохранителей: {len(off)}',
                   ', '.join(off),
                   'задайте их в панели: Управление → Портфель')


def check_copies():
    """
    Не работает ли рядом вторая копия приложения.

    ЗАЧЕМ. Разбор 364 сделок сервера за 5–29 августа 2026: две копии — из
    исходников и собранная — торговали 23 дня одновременно, каждая со своим
    счётчиком сделок и своей цепочкой баланса. 120 сигналов взяты ДВАЖДЫ: та
    же пара, тот же стоп, та же цель. Риск на идею оказался вдвое выше
    заявленного, а замеры стали смесью двух опытов с разными настройками.

    Замок пропускает такое ПО ЗАМЫСЛУ: он стережёт каталог данных, а разные
    папки друг другу не мешают. Но рынок у копий один, и заметить это можно
    было только вручную, сверяя журнал.

    Не ошибка, а предупреждение: две копии бывают нужны — например, проверить
    новую сборку рядом с рабочей. Решает человек, дело кода — показать.
    """
    import config
    import single_instance
    try:
        others = single_instance.siblings(config.DATA_DIR)
    except Exception as exc:                       # noqa: BLE001
        return _result(WARN, 'Соседние копии не проверены', str(exc), '')
    if not others:
        return _result(OK, 'Копия одна', 'других запущенных копий не найдено')
    where = '; '.join(f"PID {o.get('pid')} — {o.get('data_dir')}" for o in others)
    return _result(WARN, f'Рядом работает копий: {len(others)}', where,
                   'две копии берут одни сигналы дважды — риск на идею '
                   'удваивается, а замеры смешиваются. Оставьте одну.')


def check_strategy_knobs():
    """
    Настройки, которые обещают выбор, но выбрать по ним нельзя.

    ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА. Такая настройка хуже отсутствующей: человек
    ставит значение, ждёт другого поведения, замеряет — и получает то же
    самое. Вывод «разницы нет» при этом верен буквально и обманчив по сути.

    Первый пойманный случай — RSIBB THIN_STOP. Расширение стопа до пола даёт
    отношение риска к прибыли ниже единицы, а MIN_RR = 1.0 такое отклоняет:
    'widen' заканчивается тем же отказом, что и 'skip'.

    Второй того же рода — зона B у FIBO: имя зоны стояло литералом, и
    статистика по ней не наполнялась никогда. Он уже исправлен (v1.3.6), но
    показывает, что класс дефектов не единичный.
    """
    try:
        from rsibb import core as rsibb_core
        from rsibb import params as rsibb_params
    except Exception as exc:                       # noqa: BLE001
        return _result(WARN, 'Настройки стратегий не проверены', str(exc), '')

    dead = []
    if (getattr(rsibb_params, 'THIN_STOP', '') == 'widen'
            and rsibb_core.widen_is_inert()):
        dead.append(f'RSIBB THIN_STOP=widen при MIN_RR={rsibb_params.MIN_RR} '
                    f'— то же, что skip')
    if not dead:
        return _result(OK, 'Настройки стратегий работают',
                       'мёртвых развилок не найдено')
    return _result(WARN, f'Настроек без действия: {len(dead)}', '; '.join(dead),
                   'значение можно менять, поведение не изменится — '
                   'замер по такой настройке ничего не покажет')


def check_telegram():
    import config
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return _result(WARN, 'Telegram не настроен',
                       'уведомления о сделках приходить не будут',
                       'TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env')
    return _result(OK, 'Telegram настроен')


CHECKS = (
    ('Python', check_python),
    ('Зависимости', check_packages),
    ('Каталог данных', check_data_dir),
    ('Файл .env', check_env),
    ('Ключи биржи', check_keys),
    ('Режим', check_mode),
    ('Стратегия', check_strategy),
    ('Пул пар', check_pairs),
    ('Риск', check_risk),
    ('Предохранители', check_limits),
    ('Копии приложения', check_copies),
    ('Настройки стратегий', check_strategy_knobs),
    ('Telegram', check_telegram),
)

NETWORK_CHECKS = (
    ('Связь с биржей', check_exchange),
)


def run(network=True):
    """Все проверки по порядку. Возвращает (список результатов, есть ли fail)."""
    results = []
    checks = list(CHECKS) + (list(NETWORK_CHECKS) if network else [])
    for name, func in checks:
        try:
            item = func()
        except Exception as exc:                   # noqa: BLE001
            item = _result(FAIL, f'{name}: проверка не выполнилась', str(exc)[:200])
        item['name'] = name
        results.append(item)
        # Без зависимостей и Python остальные проверки бессмысленны:
        # они импортируют config, который импортирует dotenv.
        if item['level'] == FAIL and name in ('Python', 'Зависимости'):
            break
    return results, any(r['level'] == FAIL for r in results)


MARK = {OK: '  OK  ', WARN: ' ВНИМ ', FAIL: 'ОШИБКА'}


def main():
    print()
    print('=' * 72)
    print('ПРОВЕРКА ГОТОВНОСТИ')
    print('=' * 72)
    results, failed = run(network='--offline' not in sys.argv)
    for item in results:
        print(f'[{MARK[item["level"]]}] {item["name"]:<18} {item["title"]}')
        if item['detail']:
            print(f'{"":<28}{item["detail"]}')
        if item['fix'] and item['level'] != OK:
            print(f'{"":<28}-> {item["fix"]}')
    print('=' * 72)
    if failed:
        print('ЗАПУСКАТЬ НЕЛЬЗЯ: сначала устраните ошибки выше.')
        return 1
    warns = sum(1 for r in results if r['level'] == WARN)
    print('Готово к запуску.' + (f' Предупреждений: {warns}.' if warns else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
