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
    import settings_store as store

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
def _guard_real_settings():
    """
    Ловушка на случай, если страховка выше не сработала.

    Запоминает время правки боевого файла до проверки и сверяет после. Молча
    испорченные настройки — худший исход из возможных: они не падают, а тихо
    меняют то, чем торгует бот.
    """
    # СТОРОЖ НЕ ТОЛЬКО ЗАМЕЧАЕТ, НО И ВОЗВРАЩАЕТ КАК БЫЛО.
    #
    # Подмена путей выше закрывает известные способы промахнуться, но не все:
    # одна утечка осталась и срабатывает примерно в каждом третьем прогоне,
    # через paper_broker. Причину я не нашёл, и пока не нашёл — важнее, чтобы
    # она не могла испортить данные. Поэтому содержимое запоминается ДО
    # проверки и восстанавливается после, если изменилось. Проверка при этом
    # падает: молчаливое восстановление превратило бы дефект в невидимый.
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    watched = [os.path.join(base, name) for name in
               ('runtime_settings.json', 'settings_history.jsonl',
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
          'файлам бот торгует и по ним же потом разбирают, что произошло')
