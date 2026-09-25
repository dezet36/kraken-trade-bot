"""
Тень отвергнутого сетапа: заводится один раз, наливается и закрывается по
правилам брокера, считает R за вычетом издержек и снимается, если сетап всё
же открыт. 25.09.2026 SMC 219 раз за сутки не открыла лонги из-за своего
направленного кэпа — и оценить этот запрет было нечем.
"""

import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import risk_gate  # noqa: E402
import shadow  # noqa: E402
import strategy_profile  # noqa: E402

H = 3_600_000
START = 1_790_000_000_000


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, 'STATE_PATH', str(tmp_path / 'shadow_state.json'))
    monkeypatch.setattr(shadow, 'CSV_PATH', str(tmp_path / 'shadow_trades.csv'))
    monkeypatch.setattr(strategy_profile, 'limit_offset_pct', lambda s: 0.0)
    monkeypatch.setattr(strategy_profile, 'expiry_hours', lambda s: 12.0)
    monkeypatch.setattr(strategy_profile, 'max_hold_hours', lambda s: 0.0)
    shadow._shadows = None
    yield
    shadow._shadows = None


def signal(direction='LONG', entry=100.0, stop=98.0, targets=(104.0,), fractions=(1.0,),
           breakeven=False, pair='LINKUSDT'):
    return {'trading_pair': pair, 'setup': {'type': direction},
            'params': {'entry': entry, 'stop_loss': stop, 'tp_targets': list(targets),
                       'tp_fractions': list(fractions), 'breakeven_after_tp': breakeven}}


def run(bars, pair='LINKUSDT'):
    """bars: (часов от отказа, high, low, close)."""
    done = []
    for hours, high, low, close in bars:
        done += shadow.advance(pair, START + int(hours * H), high, low, close)
    return done


def cost(entry=100.0, stop=98.0):
    return risk_gate.entry_cost_share(entry, abs(entry - stop), config.ENTRY_COST_ROUND_TRIP)


def closed_rows():
    with open(shadow.CSV_PATH, encoding='utf-8', newline='') as fh:
        return list(csv.DictReader(fh))


class TestOneSetupOneShadow:

    def test_the_same_setup_refused_every_cycle_is_one_shadow(self):
        for _ in range(5):
            shadow.watch('SMC', signal(), 'направленный кэп', 'LONG занято 3/3', now_ms=START)
        assert len(shadow._load()) == 1
        assert shadow._load()[0]['refusals'] == 5
        assert shadow.pairs() == {'LINKUSDT': START}

    def test_a_different_stop_is_a_different_setup(self):
        shadow.watch('SMC', signal(stop=98.0), 'направленный кэп', now_ms=START)
        shadow.watch('SMC', signal(stop=97.0), 'направленный кэп', now_ms=START)
        assert len(shadow._load()) == 2


class TestItPlaysOutLikeTheBroker:

    def test_filled_then_target(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        done = run([(0.5, 101.0, 99.9, 100.5), (2, 104.5, 100.2, 104.0)])
        assert [s['outcome'] for s in done] == ['цель']
        row = closed_rows()[0]
        assert float(row['result_r']) == pytest.approx(2.0 - cost(), abs=1e-3)
        assert row['gate'] == 'направленный кэп'

    def test_filled_then_stopped(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        run([(0.5, 101.0, 99.9, 100.5), (1, 100.2, 97.5, 98.0)])
        assert float(closed_rows()[0]['result_r']) == pytest.approx(-1.0 - cost(), abs=1e-3)

    def test_the_stop_inside_the_same_candle_counts_first(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        run([(0.5, 104.5, 97.5, 100.0)])
        assert closed_rows()[0]['outcome'] == 'стоп'

    def test_never_filled_within_the_order_life(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        run([(1, 103.0, 100.5, 102.0), (13, 103.0, 100.5, 102.0)])
        row = closed_rows()[0]
        assert row['outcome'] == 'не налился' and float(row['result_r']) == 0.0

    def test_partial_exit_with_breakeven(self):
        """Половина на первой цели, стоп в безубыток, остаток выбит в ноль."""
        shadow.watch('FIBO', signal(targets=(104.0, 108.0), fractions=(0.5, 0.5), breakeven=True),
                     'предел портфеля', now_ms=START)
        run([(0.5, 101.0, 99.9, 100.5), (2, 104.5, 101.0, 104.0), (3, 104.0, 99.5, 100.0)])
        import exit_plan
        be = exit_plan.breakeven_price(100.0, True)       # вход плюс издержки круга, как у брокера
        row = closed_rows()[0]
        assert row['outcome'] == 'частично' and row['targets_hit'] == '1'
        assert float(row['result_r']) == pytest.approx(0.5 * 2.0 + 0.5 * (be - 100.0) / 2.0 - cost(),
                                                       abs=1e-3)

    def test_a_candle_seen_twice_counts_once(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        run([(0.5, 101.0, 99.9, 100.5)])
        before = dict(shadow._load()[0])
        run([(0.5, 104.5, 97.0, 100.0)])
        assert shadow._load()[0]['last_ts'] == before['last_ts']
        assert not shadow._load()[0]['outcome']


class TestAnOpenedSetupLeavesTheShadow:

    def test_opened_later_is_written_and_dropped(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        shadow.opened('SMC', 'LINKUSDT', 'LONG', 100.0, 98.0)
        assert shadow._load() == []
        assert closed_rows()[0]['outcome'] == 'открыт позже'


class TestThePanelSeesThem:

    def test_live_and_closed(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        shadow.watch('SMC', signal(pair='LTCUSDT', entry=70.0, stop=69.0, targets=(72.0,)),
                     'направленный кэп', now_ms=START)
        run([(0.5, 101.0, 99.9, 100.5), (2, 104.5, 100.2, 104.0)])
        run([(0.5, 70.5, 69.9, 70.2)], pair='LTCUSDT')
        snap = shadow.snapshot(price_of=lambda pair: 71.0)
        assert [s['pair'] for s in snap['active']] == ['LTCUSDT']
        live = snap['active'][0]
        assert live['status'] == 'в позиции'
        assert live['now_r'] == pytest.approx(1.0 - cost(70.0, 69.0) * 100 / 100, abs=0.01)
        cell = snap['closed']['SMC']['направленный кэп']
        assert cell['n'] == 1 and cell['wins'] == 1
