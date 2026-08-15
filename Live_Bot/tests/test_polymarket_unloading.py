"""
Круг обязан закрываться, а не висеть сутками.

ЧТО ПОКАЗАЛ НОЧНОЙ ПРОГОН: двенадцать исполнений, ОДИН закрытый круг, десять
незакрытых позиций и $21 замороженных денег. Бот покупал и не продавал — то
есть работал односторонним покупателем, ровно тем, чей разобранный кошелёк
держит переоценку -$8 564.

ТРИ ПРИЧИНЫ, И ВСЕ ТРИ НАШЛИСЬ ЗАМЕРОМ.

    НАКЛОН ПРОТИВ ЗАПАСА НЕ РАБОТАЛ. Полнота делилась на MM_MAX_POSITION —
    триста контрактов, число из расчёта на большой счёт. При нашем размере в
    пять доля выходила 1.7%, а сдвиг — треть десятой доли тика. Аск не
    двигался вообще, сколько бы мы ни набрали.

    СРОК УДЕРЖАНИЯ НЕ ПРОВЕРЯЛСЯ. Функция stale_positions существовала и не
    вызывалась ни разу, хотя ограничение записано в заголовке модуля.

    ОЖИДАНИЕ БЫЛО ОДНО НА ВСЕХ. Шесть часов и рынку с обещанным кругом в
    двадцать минут, и рынку с обещанными четырьмя часами.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from polymarket import params, strategy  # noqa: E402

TOP = {'bid': 0.20, 'ask': 0.24, 'mid': 0.22, 'bid_size': 100, 'ask_size': 100}
MARKET = {'tick': 0.01, 'order_min': 5, 'size': 5, 'step_ticks': 1}


class TestSkewActuallyMoves:

    def test_empty_book_quotes_both_sides_evenly(self):
        quote = strategy.desired_quote(TOP, MARKET, position=0)
        assert quote['skew'] == 0
        assert quote['bid'] == pytest.approx(0.21)
        assert quote['ask'] == pytest.approx(0.23)

    def test_one_full_quote_makes_the_closing_side_the_best_price(self):
        """
        Набрали свою котировку — закрывающая сторона обязана стать лучшей ценой
        в стакане, оставив при этом тик прибыли.
        """
        quote = strategy.desired_quote(TOP, MARKET, position=5)
        assert quote['ask'] < TOP['ask'], 'наш аск лучше рыночного'
        assert quote['ask'] == pytest.approx(0.22)
        assert quote['bid'] < 0.21, 'покупать стало менее выгодно'

    def test_two_full_quotes_unload_at_any_price(self):
        quote = strategy.desired_quote(TOP, MARKET, position=10)
        assert quote['ask'] == pytest.approx(0.21)

    def test_short_position_tilts_the_other_way(self):
        quote = strategy.desired_quote(TOP, MARKET, position=-5)
        assert quote['bid'] > 0.21, 'покупать стало выгоднее'

    def test_the_old_cap_no_longer_decides(self, monkeypatch):
        """
        Раньше сдвиг делился на триста контрактов, и весь наклон был
        украшением. Потолок остаётся для односторонней котировки, но полноту
        больше не определяет.
        """
        monkeypatch.setattr(params, 'MM_MAX_POSITION', 300)
        quote = strategy.desired_quote(TOP, MARKET, position=5)
        assert abs(quote['skew']) > 0.005, 'сдвиг больше половины тика'


class TestStalePositionGetsOut:

    def test_stale_quotes_only_the_closing_side(self):
        quote = strategy.desired_quote(dict(MARKET, stale=True) and TOP,
                                       dict(MARKET, stale=True), position=5)
        assert quote['only'] == 'ask'

    def test_stale_stands_at_the_touch_not_inside(self):
        """
        Выходим по лучшей цене, а не шагаем внутрь: цель — выйти за время
        очереди, а не за сутки. В тейкеры при этом не переходим — комиссия
        съела бы весь спред, ради которого мы стоим.
        """
        quote = strategy.desired_quote(TOP, dict(MARKET, stale=True), position=5)
        assert quote['ask'] == pytest.approx(TOP['ask'])

    def test_short_stale_closes_by_buying(self):
        quote = strategy.desired_quote(TOP, dict(MARKET, stale=True), position=-5)
        assert quote['only'] == 'bid'
        assert quote['bid'] == pytest.approx(TOP['bid'])

    def test_no_position_means_nothing_to_close(self):
        quote = strategy.desired_quote(TOP, dict(MARKET, stale=True), position=0)
        assert quote['only'] is None

    def test_the_hold_limit_is_actually_checked(self):
        """Функция существовала и не вызывалась ни разу."""
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        assert 'maker.stale_positions()' in text
        assert "market = dict(market, stale=" in text


class TestPatienceMatchesThePromise:

    def _rotate(self, monkeypatch, wait_hours, idle_hours):
        import time

        from polymarket import mm

        market = {'token_id': 'T', 'wait_hours': wait_hours,
                  'joined_ts': time.time() - idle_hours * 3600}

        class Maker:
            state = {'books': {'T': {'position': 0, 'trades': 0}}}

        monkeypatch.setattr(mm, 'select_markets', lambda **k: [])
        keep, dropped = mm.rotate(Maker(), [market])
        return keep, dropped

    def test_a_fast_market_is_left_after_three_promises(self, monkeypatch):
        """Обещали двадцать минут — через час это ошибка в три срока."""
        _, dropped = self._rotate(monkeypatch, wait_hours=1 / 3, idle_hours=1.5)
        assert len(dropped) == 1

    def test_a_fast_market_is_kept_within_its_promise(self, monkeypatch):
        keep, dropped = self._rotate(monkeypatch, wait_hours=1 / 3, idle_hours=0.5)
        assert dropped == [] and len(keep) == 1

    def test_a_slow_market_still_hits_the_overall_ceiling(self, monkeypatch):
        """Тройное обещание не должно превращаться в вечность."""
        _, dropped = self._rotate(monkeypatch, wait_hours=8, idle_hours=7)
        assert len(dropped) == 1, 'общий потолок остаётся верхней границей'

    def test_a_market_without_a_promise_uses_the_ceiling(self, monkeypatch):
        keep, _ = self._rotate(monkeypatch, wait_hours=None, idle_hours=3)
        assert len(keep) == 1
