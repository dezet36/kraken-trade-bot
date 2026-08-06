"""
Пометка сделок, открытых не своим сетапом.

Ошибка диспетчера починена, но записи в журнале остались, и по ним нельзя
сравнивать стратегии. Пометка — не косметика: без неё «какая стратегия
лучше» отвечается по данным, часть которых относится к другой стратегии.

Признак должен быть точным в обе стороны. Пропустить подменённую — оставить
враньё в сравнении. Пометить настоящую — обесценить работающую стратегию на
ровном месте.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402

LEVEL_GEO = {'lines': [{'price': 100.0, 'label': 'уровень · касаний 3'}]}
FIBO_GEO = {'bands': [{'bottom': 1, 'top': 2, 'label': 'зона A · 38.2–61.8%'}],
            'lines': [{'price': 1.0, 'label': 'начало импульса'}]}
LEVEL_WHY = 'SHORT от уровня 100.0: прокол с возвратом, объём 2.1x'
FIBO_WHY = 'LONG в зоне Zone_A, HTF BULLISH, импульс 4.2%, score 12'


def test_real_level_trade_is_not_flagged():
    assert dashboard._suspect('LEVELS', LEVEL_GEO, 'LEVEL', LEVEL_WHY) is None


def test_empty_geometry_is_flagged():
    """Так выглядит подменённая сделка в журнале: разметка пустая."""
    reason = dashboard._suspect('LEVELS', {'bands': [], 'lines': []},
                                'Zone_A', FIBO_WHY)
    assert reason and 'не своим сетапом' in reason


def test_fibo_geometry_under_levels_name_is_flagged():
    reason = dashboard._suspect('LEVELS', FIBO_GEO, 'Zone_A', FIBO_WHY)
    assert reason is not None


def test_old_record_without_geometry_but_level_wording_survives():
    """
    Запасной признак спасает записи, сделанные до появления разметки.

    Без него все старые сделки уровней пометились бы скопом, и предупреждение
    перестали бы читать — как перестают читать сигнализацию, которая воет
    всегда.
    """
    assert dashboard._suspect('LEVELS', {}, 'LEVEL', LEVEL_WHY) is None


@pytest.mark.parametrize('strategy', ['FIBO', 'SMC'])
def test_other_strategies_are_never_flagged(strategy):
    assert dashboard._suspect(strategy, {}, 'Zone_A', FIBO_WHY) is None


def test_attention_counts_suspects():
    """Предупреждение на первом экране считает и называет стратегии."""
    payload = {'closed': [
        {'strategy': 'LEVELS', 'suspect': 'открыта не своим сетапом: …'},
        {'strategy': 'LEVELS', 'suspect': 'открыта не своим сетапом: …'},
        {'strategy': 'FIBO', 'suspect': None},
    ]}
    items = dashboard._attention(payload)
    hit = [i for i in items if 'не своим сетапом' in i['text']]
    assert len(hit) == 1
    assert '2 сделок' in hit[0]['text']
    assert 'LEVELS: 2' in hit[0]['text']


def test_attention_silent_when_nothing_suspect():
    payload = {'closed': [{'strategy': 'LEVELS', 'suspect': None}]}
    items = dashboard._attention(payload)
    assert not [i for i in items if 'не своим сетапом' in i['text']]


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
