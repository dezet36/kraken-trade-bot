"""
Множитель пределов ожидания ответа модели (infra/llm_server.scaled, 25.10.2026).

С 23.10.2026 хостинг отбирал у машины 55–65% процессора, модель стала в 3–5
раз медленнее, и задачи ИИ (обзор рынка, допуск пропа, разбор дня) бросали
ответ на полпути. Множитель LLM_TIMEOUT_SCALE растягивает все пределы разом.
"""

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra import config, llm_server  # noqa: E402


def test_default_keeps_the_limits(monkeypatch):
    monkeypatch.setattr(config, 'LLM_TIMEOUT_SCALE', 1.0, raising=False)
    assert llm_server.scaled(600) == 600


def test_scale_stretches_and_never_shrinks(monkeypatch):
    monkeypatch.setattr(config, 'LLM_TIMEOUT_SCALE', 3.0, raising=False)
    assert llm_server.scaled(600) == 1800
    monkeypatch.setattr(config, 'LLM_TIMEOUT_SCALE', 0.2, raising=False)
    assert llm_server.scaled(600) == 600, 'меньше 1 — не сокращает'
    monkeypatch.setattr(config, 'LLM_TIMEOUT_SCALE', 'мусор', raising=False)
    assert llm_server.scaled(600) == 600


def test_completion_waits_the_scaled_time(monkeypatch):
    """Все вопросы идут через _completion — там и растягивается предел; таймаут называет растянутый срок."""
    monkeypatch.setattr(config, 'LLM_TIMEOUT_SCALE', 3.0, raising=False)
    monkeypatch.setattr(config, 'LLM_SERVER_URL', 'http://127.0.0.1:1', raising=False)
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(timeout)
        raise urllib.error.URLError(TimeoutError('timed out'))
    monkeypatch.setattr(llm_server.urllib.request, 'urlopen', fake_urlopen)
    with pytest.raises(RuntimeError, match='не ответил за 30 мин'):
        llm_server._completion({'prompt': 'x'}, 600)
    assert seen == [1800]
