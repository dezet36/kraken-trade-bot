"""
Общая страховка для всех проверок: тест не может тронуть боевые данные.

ЗАЧЕМ ЭТОТ ФАЙЛ ПОЯВИЛСЯ. Проверка настройки направлений записала значение в
НАСТОЯЩИЙ runtime_settings.json — тот, по которому торгует бот. Она делала
это через bot.settings, а у того модуля SETTINGS_FILE указывает на рабочую
папку. В журнале настроек осталось две записи: LEVELS переключилась в «только
лонг» и обратно. Обошлось — обе записи в одной секунде, и итоговое состояние
верное. Но если бы бот в этот момент работал, он бы на одном цикле перестал
открывать шорты по стратегии уровней, и понять почему было бы нельзя: в
журнале изменение от «оператора», которого не было.

Одной аккуратности в конкретном тесте мало: любой следующий, который тронет
настройки через уже импортированный модуль, наступит туда же. Поэтому запрет
общий и стоит здесь.

ЧТО ДЕЛАЕТСЯ. Перед КАЖДОЙ проверкой пути к данным переводятся во временную
папку — и через переменную окружения (её читают модули при импорте), и прямой
правкой уже загруженного settings_store: тесты выгружают и переимпортируют
модули, поэтому одной переменной недостаточно.

ЧЕГО ЗДЕСЬ НЕТ. Запрета писать куда-либо ещё. Это страховка от известного
способа промахнуться, а не песочница: тест, который сам откроет боевой файл
по полному пути, никто не остановит.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ПРОВЕРКИ НЕ ЧИТАЮТ .env РАЗРАБОТЧИКА. Когда в каталоге данных .env нет,
# config ищет его по обычным правилам python-dotenv — и находил Live_Bot/.env
# той машины, где идут проверки (у владельца: риск 0.5%, свой список пар).
# Эталоны решений и брокера были записаны с этими значениями и на сервере, где
# такого файла нет, расходились (нашлось 09.10.2026 на этапе 10). Явно
# указанный файл (.env в каталоге данных) читается как прежде; поиск вверх по
# папкам в проверках выключен. Подмена — до первого импорта config.
import dotenv  # noqa: E402

_load_dotenv = dotenv.load_dotenv


def _explicit_dotenv_only(dotenv_path=None, *args, **kwargs):
    return _load_dotenv(dotenv_path, *args, **kwargs) if dotenv_path else False


dotenv.load_dotenv = _explicit_dotenv_only

# И каталог данных — временный уже на СБОРЕ: config впервые импортируется,
# когда проверки ещё собираются, и без BOT_DATA_DIR каталогом становился сам
# Live_Bot — его .env читался явным путём и оставался в окружении процесса до
# конца прогона (так эталон брокера в полном прогоне видел риск 0.5%).
import tempfile  # noqa: E402

os.environ.setdefault('BOT_DATA_DIR', tempfile.mkdtemp(prefix='kraken-test-data-'))

# Проверки видят стратегии подключёнными, как в боте: сообщения стратегий идут
# через порты strategies/outbox (control/wiring.py).
from control import wiring  # noqa: E402

wiring.install()


def pytest_configure(config):
    """
    Журнал уводится во временную папку ДО СБОРА ТЕСТОВ.

    Фикстуры для этого поздно. Файлы проверок делают импорты на уровне модуля
    («from levels import core»), а те при загрузке уже пишут в журнал — всё это
    происходит на сборе, когда ни одна фикстура ещё не отработала. Через такую
    щель в боевой файл утекало по 7–8 строк с каждого такого файла.

    Хук pytest_configure вызывается раньше сбора, поэтому подмена здесь
    накрывает и импорты тоже.
    """
    import tempfile
    import logger
    logger.LOG_FILE = os.path.join(tempfile.mkdtemp(prefix='kraken-test-log-'),
                                   'bot_log.txt')


@pytest.fixture(autouse=True)
def _isolate_the_log(tmp_path, monkeypatch):
    """
    Журнал бота — во временный файл.

    ЗАЧЕМ. Переменная окружения BOT_DATA_DIR тут не спасает: logger вычисляет
    путь ОДИН РАЗ, при импорте, а импортируется он раньше любой проверки.
    Поэтому весь набор писал в настоящий Live_Bot/bot_log.txt.

    Замерено 30 августа 2026: в журнале 139 877 строк, из них 615 — одна
    только синтетика «полоса 99.0», 410 — «уровень 1.0», 162 — «код обновлён»
    из проверок обновлятора.

    ЭТО НЕ КОСМЕТИКА. Разбирая работоспособность, я нашёл в журнале 27 записей
    «тесты после обновления не прошли — код возвращён» и решил, что
    автообновление сломано. Оно исправно: это проверки обновлятора, которые
    нарочно ломают тесты, писали в боевой журнал. Диагноз по журналу стал
    невозможен — настоящие события тонут в выдуманных.
    """
    import logger
    monkeypatch.setattr(logger, 'LOG_FILE', str(tmp_path / 'bot_log.txt'))


@pytest.fixture(autouse=True)
def _isolate_bot_data(tmp_path, monkeypatch):
    """Данные бота — во временную папку, своя на каждую проверку."""
    data_dir = tmp_path / 'bot-data'
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv('BOT_DATA_DIR', str(data_dir))

    # Уже загруженные модули переменную окружения больше не читают: путь у них
    # вычислен при импорте. Переставляем явно — и через monkeypatch, чтобы
    # значение вернулось после проверки и следующая не унаследовала чужой путь.
    #
    # МОДУЛЬ ИМПОРТИРУЕМ САМИ, а не берём из sys.modules «если уже есть».
    # Первая версия делала именно так — и не работала ровно там, где нужнее:
    # в полном прогоне settings_store к этому моменту ещё не загружен, ничего
    # не переставлялось, а тест импортировал его сам и получал БОЕВЫЕ пути. За
    # вечер так набежало двенадцать записей в настоящий журнал настроек.
    store = __import__('importlib').import_module('accounts.settings_store')

    # ПЕРЕСТАВЛЯЕМ ВО ВСЕХ ЗАГРУЖЕННЫХ КОПИЯХ, а не в одной. Тесты выгружают
    # settings_store из sys.modules и импортируют заново, поэтому bot.settings
    # и свежий settings_store бывают РАЗНЫМИ объектами с одинаковым именем.
    # Патч одного из них другой не задевает — и запись уходит в боевой файл
    # через тот, до которого не дотянулись. Ровно так утёк журнал настроек:
    # сам файл настроек был подменён, а журнал писался мимо.
    targets = {id(store): store}
    for module in list(sys.modules.values()):
        inner = getattr(module, 'settings', None)
        if inner is not None and hasattr(inner, 'SETTINGS_FILE')                 and hasattr(inner, 'HISTORY_FILE'):
            targets.setdefault(id(inner), inner)

    # И сам config: остальные модули берут пути из его DATA_DIR при импорте.
    config_module = sys.modules.get('config')
    if config_module is not None:
        monkeypatch.setattr(config_module, 'DATA_DIR', str(data_dir),
                            raising=False)

    # ОБЩЕЕ ПРАВИЛО ВМЕСТО СПИСКА ФАЙЛОВ. Каждый модуль складывает свои пути в
    # собственные константы ПРИ ИМПОРТЕ: STATE_FILE, JOURNAL_CSV, PAPER_JOURNAL
    # и ещё десяток. Подменять их поимённо — гонка, которую не выиграть: новый
    # файл добавят, а сюда добавить забудут, и утечка вернётся молча. Поэтому
    # ищем любую строковую константу, ведущую в БОЕВОЙ каталог, и уводим её во
    # временный. Точечные заплатки до этого ловили утечку через раз — в
    # зависимости от того, в каком порядке пошли проверки.
    real_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for module in list(sys.modules.values()):
        path = getattr(module, '__file__', '') or ''
        if not path.startswith(real_dir):
            continue                        # чужой модуль, не наш
        for name in list(vars(module)):
            if not name.isupper():
                continue
            value = getattr(module, name, None)
            if not isinstance(value, str) or os.path.dirname(value) != real_dir:
                continue
            monkeypatch.setattr(module, name,
                                os.path.join(str(data_dir),
                                             os.path.basename(value)),
                                raising=False)

    # Счета стратегий и настройки стратегий (с 08.10.2026) путь берут из
    # config.DATA_DIR при каждом обращении — он уже переставлен выше. Кэш у них
    # свой: сбрасываем, чтобы проверка не унаследовала счета соседней.
    from accounts import books as accounts_books
    from accounts import live as accounts_live
    from accounts import onexchange as accounts_onexchange
    from accounts import paper as accounts_paper
    from strategies import settings as strategy_settings
    monkeypatch.setattr(accounts_paper, '_cache', {'key': None, 'data': None})
    monkeypatch.setattr(accounts_live, '_cache', {'key': None, 'data': None})
    # Книги торговых счетов: кэш, неотправленное, ставки фандинга и получатель
    # сообщений — свои у каждой проверки; клиенты бирж и счётчики сбоев — тоже.
    monkeypatch.setattr(accounts_books, '_cache', {'path': None, 'state': None})
    monkeypatch.setattr(accounts_books, '_outbox', [])
    monkeypatch.setattr(accounts_books, '_funding', {})
    monkeypatch.setattr(accounts_books, '_notify', None)
    monkeypatch.setattr(accounts_onexchange, '_venues', {})
    monkeypatch.setattr(accounts_onexchange, '_fails', {})
    monkeypatch.setattr(strategy_settings, '_cache', {'key': None, 'data': {}})

    for target in targets.values():
        monkeypatch.setattr(target, 'SETTINGS_FILE',
                            str(data_dir / 'runtime_settings.json'), raising=False)
        monkeypatch.setattr(target, 'HISTORY_FILE',
                            str(data_dir / 'settings_history.jsonl'), raising=False)
        # Кэш держит настройки прошлой проверки вместе с её файлом — без
        # сброса первая же load() вернула бы чужое состояние.
        monkeypatch.setattr(target, '_cache', None, raising=False)
        monkeypatch.setattr(target, '_mtime', None, raising=False)
    yield


@pytest.fixture(autouse=True)
def _restore_smc_decisions():
    """
    Параметры решений SMC — после каждой проверки как были.

    Анализ SMC пишет настройку оператора (минимальный стоп) прямо в параметры
    решений ядра — strategy_smc._apply_settings, так задумано: ядро считает по
    числам своего модуля. Но модуль один на процесс, и проверка, позвавшая
    analyze_market, оставляла там 0.8% вместо 0.5% для всех следующих: эталон
    решений расходился, если шёл после неё (08.10.2026; в полном прогоне это
    прятал алфавитный порядок файлов).
    """
    try:
        from strategies.smc import params
    except Exception:                               # noqa: BLE001
        yield
        return
    saved = {name: getattr(params, name) for name in params.DECISION if hasattr(params, name)}
    yield
    for name, value in saved.items():
        setattr(params, name, value)


@pytest.fixture(autouse=True)
def _guard_real_settings():
    """
    Ловушка на случай, если страховка выше не сработала.

    Запоминает время правки боевого файла до проверки и сверяет после. Молча
    испорченные настройки — худший исход из возможных: они не падают, а тихо
    меняют то, чем торгует бот.
    """
    # СТОРОЖ НЕ ТОЛЬКО ЗАМЕЧАЕТ, НО И ВОЗВРАЩАЕТ КАК БЫЛО.
    #
    # Содержимое запоминается ДО проверки и восстанавливается после, если
    # изменилось. Проверка при этом падает: молчаливое восстановление
    # превратило бы дефект в невидимый.
    #
    # ПРО «УТЕЧКУ ПРИМЕРНО В КАЖДОМ ТРЕТЬЕМ ПРОГОНЕ». Здесь долго стояла запись
    # о необъяснённой утечке через paper_broker. Утечки нет. 30 августа 2026
    # причина найдена: срабатывания были у paper_state.json, в leak_trace.txt
    # лежала одна машинерия pytest без единого кадра тестового кода, а в это
    # время рядом работало САМО ПРИЛОЖЕНИЕ (pythonw desktop.py) и писало это
    # состояние каждый цикл. Сторож видел чужую запись и приписывал её
    # проверке — отличить он не может, о чём и говорит текст ошибки ниже.
    #
    # Запись оставлена нарочно: ложный след в коде стоит дороже, чем его
    # отсутствие. Пока он там был, каждый следующий разбор начинался с охоты
    # за дефектом, которого нет.
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    watched = [os.path.join(base, name) for name in
               ('runtime_settings.json', 'settings_history.jsonl',
                # счета стратегий (accounts/paper.py, с 08.10.2026) и копия
                # настроек, сделанная при переносе денег в счета
                'accounts.json', 'runtime_settings.before_accounts.json',
                # адреса источников данных (data/sources.py)
                'data_sources.json',
                # торговые счета и ключи бирж (accounts/live.py), книги и
                # журнал счетов по инструкциям (accounts/manual.py)
                'trading_accounts.json', os.path.join('secrets', 'exchange_keys.json'),
                'trading_state.json', 'trading_trades.jsonl',
                'paper_trades.csv', 'paper_trades.jsonl', 'paper_state.json',
                'positions_state.json', 'pending_orders.json')]

    def snapshot():
        out = {}
        for path in watched:
            try:
                with open(path, 'rb') as fh:
                    out[path] = fh.read()
            except OSError:
                out[path] = None
        return out

    before = snapshot()
    yield
    after = snapshot()

    broken = []
    for path, was in before.items():
        if after.get(path) == was:
            continue
        # Кто именно писал — видно только отсюда. Утечка через paper_broker
        # плавала по тестам в зависимости от порядка, и без имени файла и
        # следа вызова её приходилось ловить перезапусками.
        try:
            import traceback
            frames = ''.join(traceback.format_stack()[-6:-1])
            with open(os.path.join(base, 'leak_trace.txt'), 'a',
                      encoding='utf-8') as fh:
                fh.write(f'--- {os.path.basename(path)} ---\n{frames}\n')
        except Exception:                          # noqa: BLE001
            pass
        broken.append(os.path.basename(path))
        try:
            if was is None:
                os.remove(path)
            else:
                with open(path, 'wb') as fh:
                    fh.write(was)
        except OSError:
            pass

    assert not broken, (
        'проверка изменила боевые файлы: ' + ', '.join(broken)
        + '. Содержимое возвращено как было, но так делать нельзя: по этим '
          'файлам бот торгует и по ним же потом разбирают, что произошло.\n\n'
          'ЕСЛИ РЯДОМ ЗАПУЩЕНО САМО ПРИЛОЖЕНИЕ — сначала посмотрите на него. '
          'Оно пишет paper_state.json каждый цикл, и ловушка припишет это '
          'проверке: отличить чужую запись от своей она не может. Признак — '
          'в leak_trace.txt одна машинерия pytest и ни одного кадра тестового '
          'кода. Остановите приложение и повторите прогон.')
