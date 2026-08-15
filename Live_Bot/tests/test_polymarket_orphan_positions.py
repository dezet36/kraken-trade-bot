"""
Позиция без котировки — деньги, которые никто не пытается вернуть.

ЗАМЕР ПРЯМО В РАБОТЕ: десять открытых позиций, ВОСЕМЬ из них никто не котирует,
$12.13 заморожено — треть счёта.

Причина в том, что при запуске рабочий список берётся из СВЕЖЕГО отбора. Отбор
смотрит, где выгодно вставать сейчас, и знать не знает, где мы стояли вчера. А
позиция живёт до закрытия, и закрыть её можно только котируя: наклон против
запаса работает лишь пока мы в рынке.

Пересмотр списка (rotate) эту дыру не закрывает: он бережёт позиции в уже
имеющемся списке, но вернуть в него рынок, которого там нет, не может.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polymarket import mm, params  # noqa: E402


class Maker:
    def __init__(self, books):
        self.state = {'books': books}


class TestPositionsComeBackIntoTheWorkingList:

    def test_a_held_market_is_added(self, monkeypatch):
        monkeypatch.setattr(mm, 'known_markets', lambda: {
            'HELD': {'question': 'забытый рынок', 'tick': 0.01,
                     'token_no': 'HELD_NO', 'condition_id': 'C'}})
        maker = Maker({'HELD': {'position': 5.0}})
        got = mm.with_open_positions([{'token_id': 'FRESH'}], maker)
        tokens = [m['token_id'] for m in got]
        assert tokens == ['FRESH', 'HELD']
        added = got[1]
        assert added['question'] == 'забытый рынок'
        assert added['tick'] == 0.01
        assert added['token_no'] == 'HELD_NO', 'без встречного токена не продать'

    def test_a_market_already_in_the_list_is_not_doubled(self, monkeypatch):
        monkeypatch.setattr(mm, 'known_markets', lambda: {})
        maker = Maker({'FRESH': {'position': 5.0}})
        got = mm.with_open_positions([{'token_id': 'FRESH'}], maker)
        assert len(got) == 1, 'котировать один рынок дважды нельзя'

    def test_a_closed_position_is_not_dragged_back(self, monkeypatch):
        monkeypatch.setattr(mm, 'known_markets', lambda: {})
        maker = Maker({'GONE': {'position': 0.0}})
        got = mm.with_open_positions([{'token_id': 'FRESH'}], maker)
        assert len(got) == 1

    def test_an_unknown_market_is_still_quotable(self, monkeypatch):
        """
        Рынка может не быть даже в справочнике — а закрывать позицию всё равно
        надо. Берём безопасные значения по умолчанию.
        """
        monkeypatch.setattr(mm, 'known_markets', lambda: {})
        maker = Maker({'HELD': {'position': -5.0}})
        got = mm.with_open_positions([], maker)
        assert len(got) == 1
        assert got[0]['tick'] == 0.001
        assert got[0]['size'] == params.MM_MIN_ORDER_SIZE

    def test_we_do_not_step_inside_when_only_closing(self, monkeypatch):
        """Цель здесь не заработать спред, а выйти: шаг внутрь не нужен."""
        monkeypatch.setattr(mm, 'known_markets', lambda: {})
        maker = Maker({'HELD': {'position': 5.0}})
        got = mm.with_open_positions([], maker)
        assert got[0]['step_ticks'] == 0
        assert got[0]['holding_only'] is True


class TestBothEntryPointsUseIt:

    def test_the_service_restores_them(self):
        text = open(os.path.join(ROOT, 'polymarket', 'service.py'),
                    encoding='utf-8').read()
        body = text[text.index('def _loop('):text.index('def start(')]
        assert 'mm.with_open_positions(markets, maker)' in body
        assert body.index('with_open_positions') < body.index('mm.step(')

    def test_the_command_line_run_restores_them_too(self):
        text = open(os.path.join(ROOT, 'polymarket', 'mm.py'),
                    encoding='utf-8').read()
        body = text[text.index('def main('):]
        assert 'with_open_positions(markets, maker)' in body
