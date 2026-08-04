"""
Тесты адаптера strategy_smc — контракт с trade_manager.

Проверяют, что сетап SMC превращается ровно в ту структуру, которую
исполнитель уже умеет читать. Ошибка в этом маппинге не видна ни в бэктесте
(он работает с внутренним форматом), ни в логах — она проявилась бы только
на живых деньгах: неверный ключ и сделка уходит на биржу без стопа.
"""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_smc  # noqa: E402

T0 = pd.Timestamp('2026-01-01', tz='UTC')


def make_setup(direction='BULLISH', targets=(110.0, 115.0, 120.0)):
    """Сетап SMC в том виде, в каком его отдаёт MarketContext.evaluate."""
    return {
        'pair': 'BTCUSDT',
        'index': 250,
        'time': T0,
        'direction': direction,
        'poi': {
            'type': 'ORDER_BLOCK',
            'direction': direction,
            'top': 101.0,
            'bottom': 99.0,
            'index': 240,
            'confirmed_at': 245,
            'entry_near': 101.0,
            'entry_mid': 100.0,
            'invalidation': 99.0,
            'touches': 0,
        },
        'poi_score': 1.2,
        'leg': {
            'direction': direction,
            'start': {'price': 90.0, 'index': 200, 'time': T0},
            'end': {'price': 105.0, 'index': 240, 'time': T0},
            'size': 15.0,
        },
        'sweep': {'source': 'PDL'},
        'fvg': None,
        'factors': {'liquidity_swept': True, 'poi_fresh': True, 'killzone': False},
        'confluence': 4.8,
        'params': {
            'entry': 101.0,
            'stop_loss': 98.5,
            'targets': list(targets),
            'fractions': [0.25, 0.25, 0.50],
            'rr': 4.2,
            'rr_first': 3.6,
            'rr_final': 7.6,
            'sl_distance': 2.5,
            'position_size': 40.0,
            'risk_amount': 100.0,
            'sl_mode': 'conservative',
        },
    }


class TestSignalMapping:
    def test_has_all_keys_trade_manager_reads(self):
        """
        trade_manager обращается к этим ключам напрямую. Отсутствие любого —
        KeyError в момент отправки ордера на биржу.
        """
        signal = strategy_smc._to_bot_signal(make_setup(), 'BTCUSDT', 10_000)

        assert set(signal) >= {'trading_pair', 'setup', 'trigger', 'params'}
        assert signal['trading_pair'] == 'BTCUSDT'

        for key in ('type', 'start_price', 'end_price', 'size'):
            assert key in signal['setup'], f'setup.{key} отсутствует'

        for key in ('zone', 'entry_type', 'trigger_price'):
            assert key in signal['trigger'], f'trigger.{key} отсутствует'

        for key in ('entry', 'stop_loss', 'take_profit_1', 'take_profit_2',
                    'be_level', 'position_size', 'risk_amount', 'rr', 'sl_distance'):
            assert key in signal['params'], f'params.{key} отсутствует'

    def test_direction_translated_to_bot_vocabulary(self):
        """Ядро говорит BULLISH/BEARISH, исполнитель понимает LONG/SHORT."""
        assert strategy_smc._to_bot_signal(
            make_setup('BULLISH'), 'BTCUSDT', 10_000)['setup']['type'] == 'LONG'
        assert strategy_smc._to_bot_signal(
            make_setup('BEARISH'), 'BTCUSDT', 10_000)['setup']['type'] == 'SHORT'

    def test_stop_on_correct_side_of_entry(self):
        """Стоп по ту сторону входа — иначе позиция закроется мгновенно."""
        long_signal = strategy_smc._to_bot_signal(make_setup('BULLISH'), 'BTCUSDT', 10_000)
        prm = long_signal['params']
        assert prm['stop_loss'] < prm['entry']
        assert prm['take_profit_1'] > prm['entry']

    def test_single_target_does_not_break_two_tp_contract(self):
        """
        Исполнитель всегда читает take_profit_2. При одной цели он должен
        получить валидную цену, а не None и не KeyError.
        """
        setup = make_setup(targets=(110.0,))
        signal = strategy_smc._to_bot_signal(setup, 'BTCUSDT', 10_000)

        assert signal['params']['take_profit_2'] == signal['params']['take_profit_1']

    def test_breakeven_disabled_by_measurement(self):
        """
        §14.1 методички советует переносить стоп в безубыток после первой цели,
        но бэктест это опроверг: с безубытком годовой результат падает с +88.5%
        до +54.2%. Подтянутый стоп выбивает позицию шумом коррекции раньше, чем
        она дойдёт до дальних целей, а именно дальние цели и дают прибыль при
        винрейте около 25%. Сигнал обязан нести это решение, а не отдавать его
        глобальной настройке, откалиброванной под другую стратегию.
        """
        signal = strategy_smc._to_bot_signal(make_setup(), 'BTCUSDT', 10_000)

        assert signal['params']['breakeven_after_tp'] is False
        assert signal['params']['be_level'] is None

    def test_exit_plan_travels_with_signal(self):
        """
        Весь план частичной фиксации должен доехать до исполнителя. Пока он
        брался из глобального config (одна цель на 100%), SMC исполнялась в
        конфигурации, которая по бэктесту убыточна: −9.1% против +40.6%.
        """
        signal = strategy_smc._to_bot_signal(make_setup(), 'BTCUSDT', 10_000)

        assert signal['params']['tp_targets'] == [110.0, 115.0, 120.0]
        assert signal['params']['tp_fractions'] == [0.25, 0.25, 0.50]

    def test_full_target_list_survives_for_journal(self):
        """
        Поля take_profit_1/2 остаются для журнала и Telegram, но полная картина
        сетапа должна быть видна и в блоке smc.
        """
        signal = strategy_smc._to_bot_signal(make_setup(), 'BTCUSDT', 10_000)
        assert signal['smc']['targets'] == [110.0, 115.0, 120.0]
        assert signal['smc']['fractions'] == [0.25, 0.25, 0.50]
        assert signal['smc']['poi_type'] == 'ORDER_BLOCK'


class TestFormingCandle:
    def test_drops_last_unclosed_candle(self):
        """
        Биржа отдаёт последнюю свечу незакрытой: её high/low меняются каждую
        секунду. Считать по ней структуру нельзя — зоны будут «прыгать».
        """
        df = pd.DataFrame({
            'timestamp': [T0 + pd.Timedelta(hours=i) for i in range(5)],
            'open': [1.0] * 5, 'high': [2.0] * 5,
            'low': [0.5] * 5, 'close': [1.5] * 5, 'volume': [10.0] * 5,
        })

        trimmed = strategy_smc._drop_forming_candle(df)
        assert len(trimmed) == 4
        assert trimmed['timestamp'].iloc[-1] == df['timestamp'].iloc[-2]

    def test_returns_none_when_nothing_left(self):
        df = pd.DataFrame({
            'timestamp': [T0], 'open': [1.0], 'high': [2.0],
            'low': [0.5], 'close': [1.5], 'volume': [10.0],
        })
        assert strategy_smc._drop_forming_candle(df) is None
        assert strategy_smc._drop_forming_candle(None) is None
