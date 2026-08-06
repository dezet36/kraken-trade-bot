"""
Настройка «разрешённые направления».

ЗАЧЕМ ПРОВЕРЯТЬ ТАК ПОДРОБНО. Это настройка, которая ЗАПРЕЩАЕТ сделки.
Ошибка в ней не видна: бот просто чуть реже открывается, и списать это можно
на рынок. Особенно опасны два случая — незнакомое значение в файле настроек
и чужое написание направления у SMC: и то и другое молча остановило бы
торговлю или, наоборот, пропустило бы запрещённое.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    import settings_store
    settings_store.SETTINGS_FILE = str(tmp_path / 'runtime_settings.json')
    settings_store.load(force=True)
    return settings_store


def test_default_is_both(store):
    for name in store.STRATEGIES:
        assert store.sides(name) == 'both'
        assert store.allows(name, 'LONG')
        assert store.allows(name, 'SHORT')


def test_short_only_blocks_longs(store):
    store.save({'FIBO': {'sides': 'short'}})
    assert store.sides('FIBO') == 'short'
    assert not store.allows('FIBO', 'LONG')
    assert store.allows('FIBO', 'SHORT')
    # Соседей не задело: настройка на стратегию, а не на бота целиком.
    assert store.sides('SMC') == 'both'
    assert store.allows('SMC', 'LONG')


def test_long_only_blocks_shorts(store):
    store.save({'LEVELS': {'sides': 'long'}})
    assert store.allows('LEVELS', 'LONG')
    assert not store.allows('LEVELS', 'SHORT')


def test_smc_naming_is_understood(store):
    """
    SMC называет стороны BULLISH и BEARISH.

    Без приведения написаний фильтр по 'SHORT' не узнал бы шорт SMC и
    пропустил бы его при выключенных шортах — настройка врала бы ровно у той
    стратегии, где перекос лонг/шорт и известен.
    """
    store.save({'SMC': {'sides': 'short'}})
    assert not store.allows('SMC', 'BULLISH')
    assert store.allows('SMC', 'BEARISH')


def test_unknown_value_does_not_stop_trading(store):
    """
    Опечатка в файле настроек не должна останавливать торговлю.

    «Выключить всё» — самое дорогое из возможных прочтений мусора, и именно
    оно получилось бы при наивной проверке на равенство.
    """
    store.save({'FIBO': {'sides': 'shrot'}})
    assert store.sides('FIBO') == 'both'
    assert store.allows('FIBO', 'LONG') and store.allows('FIBO', 'SHORT')


def test_unknown_direction_is_not_blocked(store):
    store.save({'FIBO': {'sides': 'short'}})
    assert store.allows('FIBO', '')
    assert store.allows('FIBO', 'НЕПОНЯТНО')


def test_setting_survives_reload(store):
    store.save({'FIBO': {'sides': 'short'}})
    store.load(force=True)
    assert store.sides('FIBO') == 'short'


def test_bot_skips_forbidden_direction(store, monkeypatch, tmp_path):
    """
    Сборка сигнала отказывает — это и есть точка, где настройка работает.

    Настройки берутся ЧЕРЕЗ bot.settings, а не свежим импортом. Соседние
    тесты выгружают settings_store из sys.modules, и свежий импорт даёт
    ДРУГОЙ объект модуля: запись пошла бы в него, а бот читал бы прежний.
    Проверка при этом падала бы в общем прогоне и проходила в одиночку —
    то есть указывала бы на несуществующий дефект.
    """
    import bot

    # Берём ТОТ экземпляр настроек, который читает бот. Пути ему уже
    # переставил conftest — руками их здесь трогать не надо: прошлая версия
    # трогала, и правка уходила в боевой журнал настроек мимо подмены.
    store = bot.settings
    store.load(force=True)
    store.save({'LEVELS': {'sides': 'long'}})
    candidate = {
        'pair': 'BTCUSDT',
        'signal': {'trading_pair': 'BTCUSDT',
                   'setup': {'type': 'SHORT'},
                   'params': {'entry': 1.0, 'stop_loss': 1.1,
                              'take_profit_1': 0.8, 'rr': 2.0},
                   'levels': {'level': 1.0, 'touches': 2}},
        'score': 1.0, 'rr': 2.0, 'df_1h': None,
    }
    signal, _df = bot._build_signal(candidate, 'LEVELS', 10_000)
    assert signal is None, 'запрещённое направление прошло'

    store.save({'LEVELS': {'sides': 'both'}})
    signal, _df = bot._build_signal(candidate, 'LEVELS', 10_000)
    assert signal is not None, 'разрешённое направление не прошло'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
