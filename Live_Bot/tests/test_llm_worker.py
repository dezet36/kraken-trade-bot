"""
Модель в отдельном процессе: её падение — именованный отказ, а не смерть бота.

Стресс-тест по замечанию стороннего разбора: дочерний процесс намеренно
падает (os._exit, как abort в llama.cpp), бот получает «модель упала», а
следующий вопрос поднимает новый процесс и получает ответ.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm_worker


@pytest.fixture(autouse=True)
def _fake_model(monkeypatch):
    monkeypatch.setattr(llm_worker, '_impl', 'fake_llm_impl:ask')
    llm_worker.stop()
    yield
    llm_worker.stop()


class TestTheWorkerSurvivesItsModel:

    def test_a_crash_is_a_named_refusal_and_the_next_call_recovers(self):
        answer, _ = llm_worker.ask('привет', timeout=60)
        assert 'ответ на' in answer
        pid_before = llm_worker._process.pid

        with pytest.raises(RuntimeError) as err:
            llm_worker.ask('crash', timeout=60)
        assert err.value.llm_gate == 'модель упала'
        assert llm_worker.crashes() >= 1
        assert not llm_worker.alive()

        answer, _ = llm_worker.ask('снова', timeout=60)
        assert 'ответ на' in answer
        assert llm_worker._process.pid != pid_before, 'процесс обязан быть новым'

    def test_a_hang_is_killed_by_the_deadline(self):
        with pytest.raises(RuntimeError) as err:
            llm_worker.ask('hang', timeout=3)
        assert err.value.llm_gate == 'модель зависла'
        assert not llm_worker.alive()

    def test_a_model_error_keeps_its_own_name(self):
        with pytest.raises(RuntimeError) as err:
            llm_worker.ask('error', timeout=60)
        assert err.value.llm_gate == 'окно контекста мало'
        assert llm_worker.alive(), 'ошибка модели — не падение процесса'

    def test_the_bot_side_names_are_broken_gates(self):
        import llm_decide
        assert 'модель упала' in llm_decide.BROKEN_GATES
        assert 'модель зависла' in llm_decide.BROKEN_GATES


class TestTheDispatch:

    def test_local_ask_routes_to_the_worker_when_isolated(self, monkeypatch):
        import llm_local
        monkeypatch.setattr(llm_local.config, 'LLM_ISOLATE', True)
        monkeypatch.setattr(llm_local, 'available', lambda: True)
        monkeypatch.setattr(llm_worker, 'ask',
                            lambda p, g, m: ('ответ', {'seconds': 1.5, 'model': 'x'}))
        assert llm_local.ask('q', None, None) == 'ответ'
        assert llm_local.last_stats()['seconds'] == 1.5

    def test_in_process_path_still_exists_for_debugging(self, monkeypatch):
        import llm_local
        monkeypatch.setattr(llm_local.config, 'LLM_ISOLATE', False)
        monkeypatch.setattr(llm_local, 'ask_in_process', lambda p, g, m: 'внутри')
        assert llm_local.ask('q') == 'внутри'
