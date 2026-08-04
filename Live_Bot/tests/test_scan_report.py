"""
Тесты воронки отсева.

Воронка отвечает на самый частый вопрос при наблюдении за ботом — «почему он
ничего не делает». Ошибка в ней не ломает торговлю, но приводит к неверному
решению: если причина отнесена не к той категории, оператор пойдёт крутить
не ту настройку.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan_report as report   # noqa: E402


class TestClassification:
    def test_specific_rule_wins_over_general(self):
        """
        Регрессия: «нога BEARISH против bias BULLISH» содержит слово «bias»
        и попадала в категорию «нет направления», хотя направление есть —
        не совпал импульс. Оператор пошёл бы менять фильтр тренда вместо
        параметров ноги.
        """
        assert report._label('нога BEARISH против bias BULLISH') == \
            'импульс против направления или не той длины'
        assert report._label('bias старшего ТФ нейтрален') == \
            'нет направления на старшем таймфрейме'

    def test_numbers_do_not_split_categories(self):
        """Порог в тексте не должен плодить отдельную строку на каждую пару."""
        a = report._label('confluence 4.2 < 4.7 (нет: ote_zone)')
        b = report._label('confluence 3.9 < 4.7 (нет: fvg_present)')

        assert a == b

    def test_signal_has_its_own_label(self):
        assert report._label(None) == 'сигнал найден'

    def test_unknown_reason_survives_readable(self):
        assert report._label('что-то новое') == 'что-то новое'


class TestAccumulation:
    def test_counts_and_examples(self):
        report.begin('TEST')
        report.record('TEST', 'BTCUSDT', 'confluence 4.2 < 4.7')
        report.record('TEST', 'ETHUSDT', 'confluence 3.9 < 4.7')
        report.record('TEST', 'SOLUSDT', None)
        report.finish('TEST')

        data = report.snapshot()['TEST']
        assert data['pairs'] == 3
        assert data['found'] == 1
        assert data['reasons'][0]['count'] == 2
        # Пример помогает понять, насколько близко пара была к сигналу
        assert 'BTCUSDT' in data['reasons'][0]['example']

    def test_new_cycle_replaces_previous(self):
        """Сводка описывает ПОСЛЕДНИЙ цикл, а не сумму за всё время."""
        report.begin('TEST')
        report.record('TEST', 'BTCUSDT', 'кулдаун активен')
        report.finish('TEST')

        report.begin('TEST')
        report.record('TEST', 'ETHUSDT', 'мало данных')
        report.finish('TEST')

        data = report.snapshot()['TEST']
        assert data['pairs'] == 1
        assert data['reasons'][0]['reason'] == 'мало истории по паре'

    def test_record_without_begin_is_ignored(self):
        """Запись вне цикла не должна ронять сканер."""
        report.record('НЕТ ТАКОЙ', 'BTCUSDT', 'что угодно')
        assert 'НЕТ ТАКОЙ' not in report.snapshot()
