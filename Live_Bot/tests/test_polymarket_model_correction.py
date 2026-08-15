"""
Поправка на ошибку модели работает в ОБЕ стороны и знает меру.

ЗАМЕР НА ЖИВЫХ ИСПОЛНЕНИЯХ показал не то, чего ждали:

    обещано 116.6 мин → вышло  1.1
    обещано  48.1 мин → вышло  0.6
    обещано 120.5 мин → вышло  0.5
    обещано  46.0 мин → вышло 72.0

Модель ПЕССИМИСТИЧНА: круги случаются быстрее расчёта. А поправка применялась
только в обратную сторону — когда модель оптимистична, — и пессимизм не
исправлялся никогда.

ЦЕНА ЭТОГО ВЫСОКА. Фильтр «круг быстрее восьми часов» снимал 315 рынков из 324,
и снимал по расчёту, который занижает скорость. Замерено, сколько рынков
проходит при разной поправке: ×1.0 — четырнадцать, ×0.5 — двадцать, ×0.25 —
двадцать семь, ×0.1 — тридцать восемь.

ОГРАНИЧИТЕЛЬ ОБЯЗАТЕЛЕН. Медиана по девяти замерам дала 0.01 — «модель врёт в
сто раз». Это не поправка, а несколько мгновенных исполнений, случившихся
оттого, что цена прошла сквозь нашу заявку. Вчетверо в любую сторону уже много.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import polymarket  # noqa: E402
from polymarket import params  # noqa: E402


def _rows(count, ratio):
    return [{'promised_seconds': 600, 'waited_seconds': 600 * ratio,
             'ratio': ratio} for _ in range(count)]


class TestCorrectionGoesBothWays:

    def test_a_pessimistic_model_is_corrected_down(self, tmp_path, monkeypatch):
        """Круги быстрее расчёта — значит рынков годится больше."""
        from polymarket import engine

        path = tmp_path / 'timing.jsonl'
        import json
        with open(path, 'w', encoding='utf-8') as fh:
            for row in _rows(25, 0.5):
                fh.write(json.dumps(row) + '\n')
        monkeypatch.setattr(engine, 'TIMING', str(path))
        assert polymarket.wait_factor() == 0.5

    def test_an_optimistic_model_is_corrected_up(self, tmp_path, monkeypatch):
        from polymarket import engine

        import json
        path = tmp_path / 'timing.jsonl'
        with open(path, 'w', encoding='utf-8') as fh:
            for row in _rows(25, 2.0):
                fh.write(json.dumps(row) + '\n')
        monkeypatch.setattr(engine, 'TIMING', str(path))
        assert polymarket.wait_factor() == 2.0

    def test_the_selector_applies_it_in_both_directions(self):
        text = open(os.path.join(ROOT, 'polymarket', 'selector.py'),
                    encoding='utf-8').read()
        assert 'if factor != 1.0:' in text, \
            'односторонняя поправка не исправляет пессимизм'


class TestTheCorrectionKnowsItsLimits:

    def test_a_wild_measurement_is_clamped(self, tmp_path, monkeypatch):
        """Медиана 0.01 — это шум, а не поправка в сто раз."""
        from polymarket import engine

        import json
        path = tmp_path / 'timing.jsonl'
        with open(path, 'w', encoding='utf-8') as fh:
            for row in _rows(25, 0.01):
                fh.write(json.dumps(row) + '\n')
        monkeypatch.setattr(engine, 'TIMING', str(path))
        assert polymarket.wait_factor() == params.MM_WAIT_FACTOR_MIN

    def test_the_other_extreme_is_clamped_too(self, tmp_path, monkeypatch):
        from polymarket import engine

        import json
        path = tmp_path / 'timing.jsonl'
        with open(path, 'w', encoding='utf-8') as fh:
            for row in _rows(25, 100.0):
                fh.write(json.dumps(row) + '\n')
        monkeypatch.setattr(engine, 'TIMING', str(path))
        assert polymarket.wait_factor() == params.MM_WAIT_FACTOR_MAX

    def test_too_few_measurements_change_nothing(self, tmp_path, monkeypatch):
        from polymarket import engine

        import json
        path = tmp_path / 'timing.jsonl'
        with open(path, 'w', encoding='utf-8') as fh:
            for row in _rows(3, 0.1):
                fh.write(json.dumps(row) + '\n')
        monkeypatch.setattr(engine, 'TIMING', str(path))
        assert polymarket.wait_factor() == 1.0


class TestAnEmptyAnswerIsNotAnEmptyAccount:
    """
    Биржа иногда отдаёт пустой список сделок на ровном месте, и панель
    показывала «токены $0.00» при тринадцати живых позициях — счёт выглядел на
    двадцать долларов беднее, чем есть.
    """

    def test_empty_trades_keep_the_previous_valuation(self):
        text = open(os.path.join(ROOT, 'polymarket', '__init__.py'),
                    encoding='utf-8').read()
        spot = text.index('done = executor.own_trades()')
        block = text[spot:spot + 1200]
        assert 'if done:' in block
        assert 'elif fresh:' in block, 'прежняя оценка должна сохраняться'
