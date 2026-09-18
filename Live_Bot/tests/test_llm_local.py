"""
Отсутствие модели — законное состояние, а не поломка.

ЗАЧЕМ ЭТО ПРОВЕРЯТЬ. Файл модели весит гигабайты и лежит только на сервере. На
машине разработки его нет: там восемь гигабайт памяти и видеокарта 2011 года,
и llama_cpp туда не поставить. Бот обязан работать там как прежде, просто без
пятой стратегии.

Если бы отсутствие модели роняло импорт или цикл, разработка стала бы
невозможна, а на сервере любой сбой загрузки останавливал бы всю торговлю — не
только стратегию LLM. Четыре остальные к модели отношения не имеют.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_local


@pytest.fixture(autouse=True)
def _clean():
    """Состояние модуля глобальное — сбрасываем до и после каждой проверки."""
    llm_local.unload()
    yield
    llm_local.unload()


class TestWithoutAModelTheBotStillWorks:

    def test_an_empty_path_means_unavailable(self, monkeypatch):
        monkeypatch.setattr(llm_local.config, 'LLM_MODEL_PATH', '')
        assert not llm_local.available()

    def test_a_missing_file_means_unavailable(self, monkeypatch):
        monkeypatch.setattr(llm_local.config, 'LLM_MODEL_PATH',
                            '/nope/qwen3-8b.gguf')
        assert not llm_local.available()

    def test_asking_raises_instead_of_returning_nothing(self, monkeypatch):
        """
        Пустой ответ неотличим от отказа модели входить. Поэтому недоступность
        — это исключение, а llm_decide превращает его в ИМЕНОВАННЫЙ отказ.
        Молчаливая пустота дала бы в журнале «модель пропустила» там, где
        модели вообще не было.
        """
        monkeypatch.setattr(llm_local.config, 'LLM_MODEL_PATH', '')
        with pytest.raises(RuntimeError):
            llm_local.ask('вопрос')

    def test_the_named_refusal_reaches_the_verdict(self, monkeypatch):
        import numpy as np
        import pandas as pd

        import llm_decide

        monkeypatch.setattr(llm_local.config, 'LLM_MODEL_PATH', '')
        idx = np.arange(400)
        closes = 100 + 4 * np.sin(idx / 23 * 2 * np.pi) + idx * 0.004
        df = pd.DataFrame({
            'timestamp': pd.to_datetime(idx * 3_600_000 + 1_700_000_000_000,
                                        unit='ms'),
            'open': closes, 'high': closes + closes * 0.002,
            'low': closes - closes * 0.002, 'close': closes,
            'volume': np.full(400, 100.0),
        })
        out = llm_decide.decide('BTCUSDT', df, llm_local.ask)
        assert not out['ok']
        assert out['gate'] == 'модель недоступна'


class TestItDoesNotRetryAHopelessLoad:
    """
    Не сумев загрузить модель один раз, не пытаемся снова каждый цикл.

    Повторная попытка читать пятигигабайтный файл раз в пять минут — это
    гарантированный тормоз на сервере, где файла попросту нет.
    """

    def test_a_failed_import_is_remembered(self, monkeypatch, tmp_path):
        fake = tmp_path / 'model.gguf'
        fake.write_bytes(b'not a model')
        monkeypatch.setattr(llm_local.config, 'LLM_MODEL_PATH', str(fake))

        calls = {'n': 0}
        real_import = __import__

        def counting(name, *args, **kwargs):
            if name == 'llama_cpp':
                calls['n'] += 1
                raise ImportError('нет такого модуля')
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr('builtins.__import__', counting)

        for _ in range(3):
            with pytest.raises(RuntimeError):
                llm_local.ask('вопрос')
        assert calls['n'] == 1, 'попытка загрузки повторялась'

    def test_unload_allows_a_fresh_attempt(self, monkeypatch, tmp_path):
        """
        Сброс нужен после замены файла модели: иначе запомненный отказ
        пережил бы исправление и стратегия осталась бы выключенной молча.
        """
        monkeypatch.setattr(llm_local.config, 'LLM_MODEL_PATH', '')
        with pytest.raises(RuntimeError):
            llm_local.ask('вопрос')
        llm_local.unload()
        assert llm_local._failed is False


class TestTheSettingsAreReadable:

    def test_defaults_exist(self):
        import config
        for name in ('LLM_MODEL_PATH', 'LLM_THREADS', 'LLM_CTX',
                     'LLM_MAX_TOKENS', 'LLM_THINK_TAG', 'LLM_TEMPERATURE'):
            assert hasattr(config, name), name

    def test_threads_leave_room_for_the_bot(self):
        """
        Модель не забирает все ядра. Замер показал упор в память, а не в счёт:
        лишние потоки скорости не дают, зато останавливают торговый цикл.
        """
        import config
        assert 1 <= config.LLM_THREADS <= 8

    def test_thinking_is_off_by_default(self):
        """
        На процессоре без видеокарты цепочка рассуждений стоит минут, а
        проверки в llm_decide всё равно пересчитывают за моделью.
        """
        import config
        assert config.LLM_THINK_TAG == '/no_think'
