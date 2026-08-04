"""
Тесты настроек, меняемых на ходу.

Эти значения управляют размером позиции. Опечатка в поле «риск» — 5 вместо
0.5 — это десятикратный объём и разорение на серии из десяти минусов, поэтому
проверка стоит на входе, а не на внимательности того, кто вводит.
"""

import json
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


class TestDefaults:
    def test_without_file_falls_back_to_env(self, store):
        import config
        data = store.load()

        assert data['FIBO']['enabled'] is True
        assert data['FIBO']['risk_pct'] == config.RISK_PER_TRADE

    def test_broken_file_does_not_stop_trading(self, store):
        """Битый JSON не должен ронять бота — торгуем на значениях из .env."""
        with open(store.SETTINGS_FILE, 'w', encoding='utf-8') as fh:
            fh.write('{ это не json')

        assert store.load(force=True)['SMC']['enabled'] is True


class TestLimits:
    def test_absurd_risk_is_clamped(self, store):
        """50% на сделку — не настройка, а опечатка."""
        result = store.save({'FIBO': {'risk_pct': 50}})

        assert result['FIBO']['risk_pct'] == store.LIMITS['risk_pct'][1]

    def test_zero_risk_is_clamped(self, store):
        result = store.save({'FIBO': {'risk_pct': 0}})

        assert result['FIBO']['risk_pct'] == store.LIMITS['risk_pct'][0]

    def test_garbage_keeps_previous_value(self, store):
        before = store.load()['SMC']['risk_pct']
        result = store.save({'SMC': {'risk_pct': 'много'}})

        assert result['SMC']['risk_pct'] == before

    def test_slots_are_whole_numbers(self, store):
        assert isinstance(store.save({'SMC': {'max_slots': 3.7}})['SMC']['max_slots'], int)


class TestPersistence:
    def test_changes_survive_reload(self, store):
        store.save({'SMC': {'enabled': False, 'risk_pct': 0.25}})
        store._cache = None
        store._mtime = None

        data = store.load(force=True)
        assert data['SMC']['enabled'] is False
        assert data['SMC']['risk_pct'] == 0.25

    def test_partial_update_keeps_the_rest(self, store):
        """Дашборд шлёт только изменённое — остальное трогать нельзя."""
        store.save({'FIBO': {'risk_pct': 0.7, 'max_slots': 4}})
        store.save({'FIBO': {'enabled': False}})

        data = store.load(force=True)
        assert data['FIBO']['risk_pct'] == 0.7
        assert data['FIBO']['max_slots'] == 4
        assert data['FIBO']['enabled'] is False

    def test_strategies_are_independent(self, store):
        store.save({'SMC': {'enabled': False}})

        assert store.enabled('FIBO') is True
        assert store.enabled('SMC') is False


class TestAccessors:
    def test_min_stop_returned_as_fraction(self, store):
        """
        В интерфейсе стоп задаётся в процентах, а расчёт умножает цену на
        долю. Перепутать — значит поставить стоп в сто раз дальше.
        """
        store.save({'FIBO': {'min_stop_pct': 1.5}})

        assert store.min_stop_pct('FIBO') == pytest.approx(0.015)
