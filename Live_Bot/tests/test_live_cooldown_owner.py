"""
Боевой путь: пауза после сделки — длиной той стратегии, которая торговала.

Кулдаун ставится при открытии, а карта «пара → стратегия» очищается при
закрытии — то есть ровно тогда, когда пауза начинается. Пока стратегия не
запоминалась вместе с отметкой, проверка после выхода видела «владельца
нет» и брала общие 12 ч из config — у Боллинджера вместо его 2 ч, у уровней
вместо 6 ч. Найдено ревью 21.09.2026, фантомный брокер этим не страдает:
у него книги раздельные.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_manager  # noqa: E402
import strategy_profile  # noqa: E402


def make(tmp_path):
    inst = trade_manager.LiveTradeManager.__new__(trade_manager.LiveTradeManager)
    inst.cooldown_file = str(tmp_path / 'cooldown_state.json')
    inst.strategy_file = str(tmp_path / 'pair_strategy.json')
    inst.pair_strategy = {}
    inst.last_trade_time = inst._load_cooldown_state()
    return inst


class TestThePauseBelongsToWhoeverTraded:

    def test_after_close_the_strategy_is_still_known(self, tmp_path):
        inst = make(tmp_path)
        inst.set_pair_strategy('DOTUSDT', 'RSIBB')
        inst._stamp_cooldown('DOTUSDT', 'RSIBB')
        inst.clear_pair_strategy('DOTUSDT')            # позиция закрыта
        assert inst._cooldown_owner('DOTUSDT') == 'RSIBB'

    def test_the_pause_has_the_length_of_that_strategy(self, tmp_path):
        inst = make(tmp_path)
        inst._stamp_cooldown('DOTUSDT', 'RSIBB')
        rsibb = strategy_profile.cooldown_hours('RSIBB')
        common = strategy_profile.cooldown_hours('FIBO')
        assert rsibb < common, 'проверка имеет смысл, только если паузы разные'
        inst.last_trade_time['DOTUSDT'] = datetime.now() - timedelta(hours=rsibb + 0.1)
        assert inst.check_cooldown('DOTUSDT') is True, 'своя пауза прошла — пара свободна'
        inst.last_trade_time['DOTUSDT'] = datetime.now() - timedelta(hours=rsibb - 0.1)
        assert inst.check_cooldown('DOTUSDT') is False

    def test_it_survives_a_restart(self, tmp_path):
        inst = make(tmp_path)
        inst._stamp_cooldown('DOTUSDT', 'LEVELS')
        again = make(tmp_path)
        assert again._cooldown_owner('DOTUSDT') == 'LEVELS'
        assert again.last_trade_time['DOTUSDT'] is not None

    def test_the_old_file_format_still_loads(self, tmp_path):
        with open(tmp_path / 'cooldown_state.json', 'w') as fh:
            json.dump({'DOTUSDT': datetime.now().isoformat()}, fh)
        inst = make(tmp_path)
        assert inst.last_trade_time['DOTUSDT'] is not None
        assert inst._cooldown_owner('DOTUSDT') is None          # неизвестно — общая пауза
        assert inst.check_cooldown('DOTUSDT') is False

    def test_a_pair_never_traded_is_free(self, tmp_path):
        inst = make(tmp_path)
        assert isinstance(inst.last_trade_time, defaultdict)
        assert inst.check_cooldown('NEVERUSDT') is True
