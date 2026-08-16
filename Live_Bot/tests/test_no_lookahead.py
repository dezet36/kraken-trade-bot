"""
Заявка не наливается ходом цены, которого мы не застали.

ОТКУДА ЭТО. В журнале у четырёх сделок время ожидания входа отрицательное:
−2 и −3 минуты. Заявка не может исполниться раньше, чем выставлена, — значит
считалась она по свече, открывшейся ДО постановки.

Так и было. При постановке заявке присваивалось `last_ts = now - BAR_MS` с
пометкой «начинаем со свечи, идущей сейчас», а свеча, идущая сейчас, открылась
в прошлом. Заявка, выставленная в 10:03, попадала на свечу 10:00–10:05 и
наливалась, если цена задела лимит в 10:01 — когда заявки ещё не существовало.

Итог этих четырёх сделок — −$52.49 при одном плюсе из четырёх: выборке дефект
не польстил. Но завышает он не прибыль, а ДОЛЮ ИСПОЛНЕНИЙ, и в этом его вред:
часть сетапов выглядела рабочей за счёт хода, которого мы не застали.

Плата за честность — до одной свечи задержки перед первой проверкой.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BAR_MS = 300_000


class FakeClient:
    def __init__(self):
        self.candles = {}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        rows = list(self.candles.get(symbol, []))
        if since is not None:
            rows = [c for c in rows if c[0] >= since]
        return rows[:limit] if limit else rows

    def fetch_funding_rate(self, symbol):
        raise RuntimeError('ставка недоступна')


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Прежние модули возвращаются на место — см. фикстур в test_candle_gap."""
    monkeypatch.setenv('BOT_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TRADING_MODE', 'PAPER')
    monkeypatch.setenv('PAPER_START_BALANCE', '10000')
    monkeypatch.setenv('PAPER_FUNDING', '0')
    saved = {m: sys.modules.pop(m, None) for m in ('config', 'paper_broker')}
    import paper_broker
    yield paper_broker
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def signal(entry=100.0, stop=90.0, tp1=130.0, pair='BTCUSDT'):
    return {
        'trading_pair': pair,
        'strategy': 'FIBO',
        'setup': {'type': 'LONG', 'start_price': 80.0, 'end_price': 120.0,
                  'size': 40.0},
        'trigger': {'zone': 'Zone_A'},
        'htf_trend': 'BULLISH',
        'scan': {'score': 71.5, 'proximity': 0.8},
        'params': {
            'entry': entry, 'stop_loss': stop,
            'take_profit_1': tp1, 'take_profit_2': tp1,
            'be_level': None, 'breakeven_after_tp': True,
            'max_same_direction': 0,
            'tp_targets': [tp1], 'tp_fractions': [1.0],
            'rr': abs(tp1 - entry) / abs(entry - stop),
            'position_size': 1.0, 'risk_amount': 100.0,
        },
    }


class TestAFreshOrderIgnoresTheCandleItWasBornIn:

    def test_the_candle_that_opened_before_placement_is_not_processed(self, env,
                                                                     monkeypatch):
        """
        РОВНО ТОТ ДЕФЕКТ. Заявка выставлена в середине свечи; эта свеча своим
        минимумом накрывает лимит. Наливаться нечему: минимум мог случиться
        до постановки, и знать этого мы не можем.
        """
        bar_open = 1_700_000_000_000
        placed = bar_open + 3 * 60_000                # 10:03 при свече на 10:00
        monkeypatch.setattr(env, '_now_ms', lambda: placed)

        broker = env.PaperBroker(FakeClient(), strategies=('FIBO',))
        assert broker.open('FIBO', signal(entry=100.0))
        order = broker.pending('FIBO')['BTCUSDT']

        assert not (bar_open > order['last_ts']), (
            f"свеча, открывшаяся за {(placed - bar_open) // 60000} мин до "
            f"постановки, попала бы в обработку — это заглядывание вперёд")

    def test_the_next_candle_is_processed(self, env, monkeypatch):
        """Правило обязано резать только прошлое, иначе заявка не наливается."""
        bar_open = 1_700_000_000_000
        placed = bar_open + 3 * 60_000
        monkeypatch.setattr(env, '_now_ms', lambda: placed)

        broker = env.PaperBroker(FakeClient(), strategies=('FIBO',))
        broker.open('FIBO', signal(entry=100.0))
        order = broker.pending('FIBO')['BTCUSDT']

        assert bar_open + BAR_MS > order['last_ts'], (
            'следующая свеча обязана обрабатываться, иначе заявка мертва')

    def test_a_candle_opening_exactly_at_placement_is_processed(self, env,
                                                                monkeypatch):
        """Совпадение с точностью до миллисекунды — это уже не прошлое."""
        placed = 1_700_000_000_000
        monkeypatch.setattr(env, '_now_ms', lambda: placed)
        broker = env.PaperBroker(FakeClient(), strategies=('FIBO',))
        broker.open('FIBO', signal(entry=100.0))
        assert placed > broker.pending('FIBO')['BTCUSDT']['last_ts']


class TestTheFillIsNeverStampedBeforeThePlacement:
    """
    Смысловая проверка: отрицательное ожидание входа в журнале — признак того,
    что дефект вернулся. Именно по нему он и был найден.
    """

    def test_wait_cannot_come_out_negative(self, env, monkeypatch):
        bar_open = 1_700_000_000_000
        placed = bar_open + 3 * 60_000
        monkeypatch.setattr(env, '_now_ms', lambda: placed)

        client = FakeClient()
        broker = env.PaperBroker(client, strategies=('FIBO',))
        broker.open('FIBO', signal(entry=100.0))
        order = broker.pending('FIBO')['BTCUSDT']

        # Обе свечи накрывают лимит своим минимумом: первая открылась ДО
        # постановки и обязана быть пропущена, вторая — после.
        bars = [
            [bar_open, 101.0, 105.0, 99.0, 101.0, 0],
            [bar_open + BAR_MS, 101.0, 105.0, 99.0, 101.0, 0],
        ]
        broker._advance('FIBO', 'BTCUSDT', bars, 0.0)

        pos = broker.positions('FIBO').get('BTCUSDT')
        assert pos is not None, 'заявка обязана была налиться на второй свече'
        wait = int((pos['opened_ts'] - pos['placed_ts']) / 60000)
        assert wait >= 0, (
            f'ожидание входа {wait} мин — заявка исполнилась раньше, чем '
            f'выставлена')
        assert pos['opened_ts'] == bar_open + BAR_MS, (
            'налилась не на той свече: взята та, что открылась до постановки')
