"""
Подключение стратегии Боллинджера к боту.

ЗАЧЕМ ЭТИ ТЕСТЫ. Ровно на этом месте проект однажды потерял месяц: диспетчер
не имел ветки под стратегию уровней, отдавал её кандидатов ветке Фибоначчи, и
та искала на тех же свечах СВОЙ сетап, помечая результат чужим именем. Ошибка
не падала и в журнале выглядела как нормальная работа.

Четвёртая стратегия — первая проверка того, что урок усвоен: она должна либо
обслуживаться своей веткой, либо получать явный отказ. Третьего не дано.

Второе, что проверяется, — обязательные поля общего договора. У стратегии
уровней однажды забыли `trigger`, и первый же реальный вход упал бы с
KeyError. Здесь это ловится тестом, а не первой сделкой.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def bot(monkeypatch, tmp_path):
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    for module in ('config', 'settings_store'):
        sys.modules.pop(module, None)
    import bot as module
    import settings_store
    settings_store.SETTINGS_FILE = str(tmp_path / 'runtime_settings.json')
    settings_store._cache = None
    settings_store._mtime = None
    for name in ('bot', 'settings_store'):
        loaded = sys.modules.get(name)
        if loaded is not None and hasattr(loaded, 'settings'):
            loaded.settings = settings_store
    return module


def a_signal(direction='LONG'):
    """Сигнал в том виде, в каком его отдаёт сканер Боллинджера."""
    return {
        'trading_pair': 'BTCUSDT',
        'setup': {'type': direction, 'start_price': 99.0, 'end_price': 100.0,
                  'size': 1.0, 'start_time': '2026-08-01T00:00:00Z'},
        'params': {'entry': 99.0, 'stop_loss': 98.0, 'take_profit_1': 100.0,
                   'take_profit_2': 100.0, 'tp_targets': [100.0],
                   'tp_fractions': [1.0], 'be_level': None,
                   'breakeven_after_tp': False, 'max_same_direction': 0,
                   'risk_pct': 0.5, 'position_size': 1.0, 'risk_amount': 50.0,
                   'rr': 1.0, 'sl_distance': 1.0},
        'trigger': {'zone': 'BAND', 'entry_type': 'LIMIT', 'trigger_price': 99.0},
        'zone': 'BAND', 'htf_trend': 'NEUTRAL', 'score': 20.0, 'why': 'тест',
        'rsibb': {'band': 99.0, 'mid': 100.0, 'upper': 101.0, 'lower': 99.0,
                  'rsi': 60.0, 'adx': 18.0, 'width_ratio': 1.0, 'rr': 1.0,
                  'stop_pct': 1.0},
    }


class TestDispatcher:
    def test_own_branch_keeps_the_scanner_signal(self, bot):
        """
        Готовый сигнал должен дойти неизменным, а не быть пересобран чужой
        стратегией. Именно подмена и была той ошибкой на месяц.
        """
        signal, _df = bot._build_signal(
            {'pair': 'BTCUSDT', 'signal': a_signal(), 'score': 20.0, 'rr': 1.0,
             'df_1h': None}, 'RSIBB', 10_000)
        assert signal is not None
        assert signal['strategy'] == 'RSIBB'
        assert signal['zone'] == 'BAND'
        assert signal['params']['entry'] == 99.0
        assert signal['scan']['rsi'] == 60.0

    def test_candidate_without_signal_is_refused(self, bot):
        signal, _df = bot._build_signal(
            {'pair': 'BTCUSDT', 'score': 1.0, 'rr': 1.0, 'df_1h': None},
            'RSIBB', 10_000)
        assert signal is None

    def test_unknown_strategy_still_refused(self, bot):
        """Пятая стратегия не должна тихо достаться Фибоначчи."""
        signal, _df = bot._build_signal(
            {'pair': 'BTCUSDT', 'signal': a_signal(), 'score': 1.0, 'rr': 1.0,
             'df_1h': None}, 'НЕИЗВЕСТНАЯ', 10_000)
        assert signal is None

    def test_sides_setting_applies(self, bot):
        import settings_store
        settings_store.save({'RSIBB': {'sides': 'short'}})
        signal, _df = bot._build_signal(
            {'pair': 'BTCUSDT', 'signal': a_signal('LONG'), 'score': 1.0,
             'rr': 1.0, 'df_1h': None}, 'RSIBB', 10_000)
        assert signal is None
        signal, _df = bot._build_signal(
            {'pair': 'BTCUSDT', 'signal': a_signal('SHORT'), 'score': 1.0,
             'rr': 1.0, 'df_1h': None}, 'RSIBB', 10_000)
        assert signal is not None


class TestContract:
    """Поля, которые читают шесть разных мест. Отсутствие любого — KeyError."""

    def test_signal_carries_every_required_field(self):
        import strategy_rsibb
        from rsibb import core

        size = 80
        rng = np.random.default_rng(11)
        close = 100 + np.cumsum(rng.normal(0, 0.5, size))
        ind = core.indicators(close, close + 0.5, close - 0.5, close)
        # Заставляем последний бар быть сетапом: цена под нижней полосой,
        # RSI выше 50 — то самое расхождение, которое стратегия и торгует.
        at = size - 1
        ind['lower'][at] = close[at] + 1.0
        ind['upper'][at] = close[at] + 4.0
        ind['mid'][at] = close[at] + 2.5
        ind['width'][at] = 3.0
        ind['low'][at] = close[at]
        ind['rsi'][at] = 62.0
        ind['adx'][at] = 18.0
        ind['width_ratio'][at] = 1.0

        setup, why = core.evaluate(ind, at)
        assert setup is not None, why
        trade = core.build_trade(setup)
        assert trade is not None

        import pandas as pd
        df = pd.DataFrame({'timestamp': pd.date_range('2026-08-01', periods=size,
                                                      freq='h')})
        signal = strategy_rsibb._to_bot_signal(setup, trade, 'BTCUSDT', 10_000, df)

        for field in ('trading_pair', 'setup', 'params', 'trigger', 'zone',
                      'htf_trend', 'score', 'why', 'rsibb'):
            assert field in signal, field
        for field in ('entry', 'stop_loss', 'take_profit_1', 'tp_targets',
                      'tp_fractions', 'sl_distance', 'rr', 'position_size'):
            assert field in signal['params'], field
        # Вход обязан быть лимитным: вся арифметика издержек держится на
        # мейкерской комиссии, вход по рынку сделал бы стратегию убыточной.
        assert signal['trigger']['entry_type'] == 'LIMIT'
        assert signal['setup']['type'] == 'LONG'


class TestRegistration:
    def test_strategy_known_everywhere(self):
        import paper_broker
        import settings_store
        assert 'RSIBB' in settings_store.STRATEGIES
        assert 'RSIBB' in paper_broker.STRATEGIES

    def test_dashboard_knows_name_and_colour(self):
        page = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'dashboard.html')
        text = open(page, encoding='utf-8').read()
        assert 'RSIBB:' in text
        assert '--rsibb:' in text
        # Цвет обязан быть задан в ОБЕИХ темах: тёмная не осветлённая светлая,
        # и подобранный для одной в другой сливается с синим.
        assert text.count('--rsibb:') >= 3
