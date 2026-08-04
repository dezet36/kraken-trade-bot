"""
Тесты словаря человеческих формулировок.

Словарь попадает не только на экран, но и в выгрузку CSV, которую открывают в
Excel. Ошибка здесь не ломает торговлю, но вводит в заблуждение при разборе:
стоп после двух взятых целей — это частично зафиксированная прибыль, а не
убыток, и путать их в отчёте нельзя.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import glossary   # noqa: E402


class TestExitReasons:
    def test_plain_reasons(self):
        assert glossary.exit_reason('SL') == 'стоп-лосс'
        assert glossary.exit_reason('TP3') == 'третья цель'
        assert glossary.exit_reason('TIME') == 'тайм-стоп'

    def test_stop_after_partial_targets_is_not_a_plain_loss(self):
        """
        «SL_after_TP2» означает, что три четверти позиции уже зафиксированы в
        плюс. Показать это просто как «стоп» — исказить разбор сделки.
        """
        text = glossary.exit_reason('SL_after_TP2')

        assert 'стоп' in text and 'вторая цель' in text

    def test_unknown_reason_survives(self):
        assert glossary.exit_reason('ЧТО-ТО') == 'ЧТО-ТО'

    def test_empty_reason(self):
        assert glossary.exit_reason('') == '—'


class TestConfirmations:
    def test_splits_fired_and_missing(self):
        """
        Несработавшие подтверждения не менее важны сработавших: именно они
        объясняют, почему две внешне одинаковые сделки разошлись.
        """
        ok, missing = glossary.confirmations({
            'ote_zone': True, 'poi_fresh': True,
            'liquidity_swept': False, 'killzone': False,
        })

        assert 'вход в зоне OTE (0.62–0.79)' in ok
        assert 'зона ещё не тронута' in ok
        assert 'ликвидность снята перед входом' in missing
        assert len(ok) == 2 and len(missing) == 2

    def test_accepts_plain_list_of_fired(self):
        """Старый формат сигнала — просто список сработавших."""
        ok, missing = glossary.confirmations(['ote_zone', 'fvg_present'])

        assert len(ok) == 2 and missing == []

    def test_empty_input(self):
        assert glossary.confirmations(None) == ([], [])


class TestNames:
    def test_known_terms_translated(self):
        assert glossary.poi_type('ORDER_BLOCK') == 'ордер-блок'
        assert glossary.zone('Zone_A').startswith('зона A')
        assert glossary.trend('BULLISH') == 'восходящий'
        assert glossary.direction('SHORT') == 'шорт'

    def test_unknown_term_returned_as_is(self):
        """Новый тип зоны не должен исчезнуть с экрана из-за отсутствия перевода."""
        assert glossary.poi_type('НОВЫЙ_ТИП') == 'НОВЫЙ_ТИП'
