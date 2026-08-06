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
    store = sys.modules.get('settings_store')
    if store is not None:
        monkeypatch.setattr(store, 'SETTINGS_FILE',
                            str(data_dir / 'runtime_settings.json'),
                            raising=False)
        monkeypatch.setattr(store, 'HISTORY_FILE',
                            str(data_dir / 'settings_history.jsonl'),
                            raising=False)
        # Кэш держит настройки прошлой проверки вместе с её файлом — без
        # сброса первая же load() вернула бы чужое состояние.
        monkeypatch.setattr(store, '_cache', None, raising=False)
        monkeypatch.setattr(store, '_mtime', None, raising=False)
    yield


@pytest.fixture(autouse=True)
def _guard_real_settings():
    """
    Ловушка на случай, если страховка выше не сработала.

    Запоминает время правки боевого файла до проверки и сверяет после. Молча
    испорченные настройки — худший исход из возможных: они не падают, а тихо
    меняют то, чем торгует бот.
    """
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'runtime_settings.json')
    before = os.path.getmtime(real) if os.path.exists(real) else None
    yield
    after = os.path.getmtime(real) if os.path.exists(real) else None
    assert before == after, (
        'проверка изменила БОЕВОЙ runtime_settings.json — так нельзя: '
        'бот торгует по этому файлу')
