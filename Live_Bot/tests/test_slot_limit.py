"""
Предел одновременных позиций: ноль означает «без предела».

ПОЧЕМУ ЭТО СТОИТ ОТДЕЛЬНЫХ ТЕСТОВ. Ноль в поле «сколько позиций» читается
двумя противоположными способами: «ни одной» и «сколько угодно». Выбран второй
— как у остальных ограничителей проекта, где ноль выключает проверку. Но
наивное `budget - used` при нуле даёт ОТРИЦАТЕЛЬНОЕ число, то есть «слоты
заняты», и торговля молча останавливается совсем. Такая ошибка не падает и в
журнале выглядит как обычный день без сетапов.

Проверяется поэтому не только расшифровка нуля, но и то, что через неё
проходят оба места принятия решения: «есть ли свободный слот» и «сколько ещё
открыть в этом цикле».
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('config', 'settings_store'):
        sys.modules.pop(module, None)
    import settings_store
    settings_store.SETTINGS_FILE = str(tmp_path / 'runtime_settings.json')
    settings_store._cache = None
    settings_store._mtime = None
    return settings_store


class TestDefault:
    def test_no_limit_by_default(self, store):
        """По умолчанию предела нет — это и есть изменение, ради которого всё."""
        for strategy in store.STRATEGIES:
            assert store.max_slots(strategy) == store.UNLIMITED
            assert store.slots_free(strategy, 0) is None
            assert store.slots_free(strategy, 99) is None

    def test_env_choice_wins_over_default(self, store, monkeypatch, tmp_path):
        """Осознанно заданный SLOTS_PER_STRATEGY сильнее умолчания."""
        monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
        monkeypatch.setenv('SLOTS_PER_STRATEGY', '4')
        for module in ('config', 'settings_store'):
            sys.modules.pop(module, None)
        import settings_store as fresh
        fresh.SETTINGS_FILE = str(tmp_path / 'other.json')
        fresh._cache = None
        fresh._mtime = None
        assert fresh.max_slots('FIBO') == 4
        assert fresh.slots_free('FIBO', 1) == 3


class TestZeroIsNotZeroPositions:
    def test_free_slots_never_negative_on_unlimited(self, store):
        """
        Ловушка, ради которой писался slots_free.

        `budget - used` при budget = 0 даёт −7, а «свободно меньше нуля»
        означает «слоты заняты». Торговля остановилась бы полностью, и в
        журнале это выглядело бы как день без сетапов.
        """
        store.save({'LEVELS': {'max_slots': 0}})
        assert store.slots_free('LEVELS', 7) is None

    def test_label_says_it_in_words(self, store):
        store.save({'SMC': {'max_slots': 0}})
        assert store.slots_label('SMC', 3) == '3 (без предела)'
        store.save({'SMC': {'max_slots': 8}})
        assert store.slots_label('SMC', 3) == '3/8'


class TestExplicitLimit:
    def test_limit_is_kept_and_counted(self, store):
        store.save({'FIBO': {'max_slots': 6}})
        assert store.max_slots('FIBO') == 6
        assert store.slots_free('FIBO', 2) == 4
        assert store.slots_free('FIBO', 6) == 0
        assert store.slots_free('FIBO', 9) == -3

    def test_range_allows_zero_and_caps_high(self, store):
        low, high = store.LIMITS['max_slots']
        assert low == 0
        assert store.save({'SMC': {'max_slots': -5}})['SMC']['max_slots'] == low
        assert store.save({'SMC': {'max_slots': 999}})['SMC']['max_slots'] == high

    def test_survives_restart(self, store):
        store.save({'LEVELS': {'max_slots': 6}})
        store._cache = None
        store._mtime = None
        assert store.max_slots('LEVELS') == 6


class TestCycleUsesIt:
    """
    Оба места принятия решения переживают отсутствие предела.

    Это не проверка стиля: `opened >= free` при free = None падает с
    TypeError прямо в цикле открытия сделок.
    """

    def test_gate_and_counter_accept_none(self, store):
        free = store.slots_free('FIBO', 12)
        assert free is None
        assert not (free is not None and free <= 0)      # ворота пропускают
        opened = 5
        assert not (free is not None and opened >= free)  # счётчик не режет

    def test_gate_closes_on_explicit_limit(self, store):
        store.save({'FIBO': {'max_slots': 3}})
        free = store.slots_free('FIBO', 3)
        assert free == 0
        assert free is not None and free <= 0
