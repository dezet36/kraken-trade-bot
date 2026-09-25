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

    def test_a_second_setup_on_the_same_pair_waits_like_at_the_broker(self):
        """
        Пока по паре живёт тень, другой сетап по ней тени не получает: брокер
        с поставленной заявкой ответил бы «уже есть заявка». Другая пара —
        своя тень.
        """
        shadow.watch('SMC', signal(stop=98.0), 'направленный кэп', now_ms=START)
        shadow.watch('SMC', signal(stop=97.0), 'направленный кэп', now_ms=START)
        shadow.watch('SMC', signal(pair='LTCUSDT'), 'направленный кэп', now_ms=START)
        assert [(s['pair'], s['stop0']) for s in shadow._load()] == [
            ('LINKUSDT', 98.0), ('LTCUSDT', 98.0)]


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

    def test_a_target_reached_before_the_entry_ends_it(self):
        """
        Брокер снимает заявку, когда цена дошла до первой цели, не задев входа
        («цена дошла до цели без нас»), — тень тоже.
        """
        shadow.watch('FIBO', signal(), 'предел портфеля', now_ms=START)
        done = run([(1, 103.0, 101.0, 102.5), (2, 104.5, 102.0, 104.2)])
        assert [s['outcome'] for s in done] == ['цель без входа']
        row = closed_rows()[0]
        assert float(row['result_r']) == 0.0 and row['closed_hours'] == '2.0'
        # Ближе всего — 101 (1% над входом); без нас — до 104.5, это +2.25R.
        assert float(row['min_gap_pct']) == pytest.approx(1.0)
        assert float(row['best_run_r']) == pytest.approx(2.25)

    def test_an_smc_order_waits_past_its_target_like_in_its_backtest(self):
        """
        У SMC заявка у цели не снимается (strategy_profile.drops_at_target):
        её бэктест так не делал, и замер 25.09.2026 показал, что снятие
        отнимает у неё лучшие сделки. Тень ждёт отката и наливается.
        """
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        assert run([(1, 104.5, 102.0, 104.2)]) == []          # цель без входа — ждёт
        done = run([(3, 102.0, 99.8, 100.4), (5, 104.6, 100.3, 104.4)])
        assert [s['outcome'] for s in done] == ['цель']
        assert float(closed_rows()[0]['fill_hours']) == 3.0

    def test_entry_and_target_in_one_candle_is_a_fill(self):
        """Как у брокера: чтобы дойти до цели, цена прошла через вход."""
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        done = run([(1, 104.5, 99.9, 104.0)])
        assert [s['outcome'] for s in done] == ['цель']

    def test_a_breakout_entry_fills_on_the_breakout(self, monkeypatch):
        """Вход по ходу движения (стоп-заявка уровней) наливается пробоем, не откатом."""
        sig = signal(entry=100.0, stop=98.0, targets=(106.0,))
        sig['trigger'] = {'entry_type': 'STOP'}
        shadow.watch('LEVELS', sig, 'предел портфеля', now_ms=START)
        run([(1, 99.8, 97.0, 99.5)])                   # откат ниже входа — не вход
        assert not shadow._load()[0]['filled']
        run([(2, 100.4, 99.6, 100.3)])                 # пробой — вход
        assert shadow._load()[0]['filled']

    def test_a_gap_through_a_breakout_entry_costs_more_than_one_r(self):
        """При разрыве стоп-заявка исполняется по открытию — как у брокера."""
        sig = signal(entry=100.0, stop=98.0, targets=(106.0,))
        sig['trigger'] = {'entry_type': 'STOP'}
        shadow.watch('LEVELS', sig, 'предел портфеля', now_ms=START)
        shadow.advance('LINKUSDT', START + H, 101.5, 101.0, 101.2, open_price=101.0)
        shadow.advance('LINKUSDT', START + 2 * H, 101.2, 97.9, 98.0, open_price=101.1)
        # Вход 101 вместо 100, стоп 98 при задуманном риске 2: −1.5R.
        assert float(closed_rows()[0]['result_r']) == pytest.approx(-1.5 - cost(), abs=1e-3)

    def test_a_candle_seen_twice_counts_once(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        run([(0.5, 101.0, 99.9, 100.5)])
        before = dict(shadow._load()[0])
        run([(0.5, 104.5, 97.0, 100.0)])
        assert shadow._load()[0]['last_ts'] == before['last_ts']
        assert not shadow._load()[0]['outcome']


class TestTheBrokersCooldown:
    """
    Без кулдауна закрытая «цель без входа» заводилась бы заново каждые пять
    минут: SMC предлагает тот же сетап каждый цикл. Брокер ставит паузу по
    паре при постановке заявки и при выходе — тень тоже.
    """

    @pytest.fixture(autouse=True)
    def _cooldown(self, monkeypatch):
        monkeypatch.setattr(strategy_profile, 'cooldown_hours', lambda s: 12.0)

    def test_no_new_shadow_within_the_cooldown_after_a_miss(self):
        shadow.watch('FIBO', signal(), 'предел портфеля', now_ms=START)
        run([(1, 104.5, 102.0, 104.2)])                # цель без входа
        shadow.watch('FIBO', signal(), 'предел портфеля', now_ms=START + 2 * H)
        assert shadow._load() == []
        # Пауза — от постановки: через 12 ч тень заводится снова.
        shadow.watch('FIBO', signal(), 'предел портфеля', now_ms=START + 12 * H + 1)
        assert len(shadow._load()) == 1

    def test_a_filled_shadow_restarts_the_pause_at_its_exit(self):
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START)
        run([(0.5, 101.0, 99.9, 100.5), (10, 100.2, 97.5, 98.0)])   # вход, стоп на 10-м часу
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START + 13 * H)
        assert shadow._load() == [], 'пауза идёт от выхода: 10 ч + 12 ч'
        shadow.watch('SMC', signal(), 'направленный кэп', now_ms=START + 22 * H + 1)
        assert len(shadow._load()) == 1

    def test_the_pause_survives_a_restart(self):
        shadow.watch('FIBO', signal(), 'предел портфеля', now_ms=START)
        run([(1, 104.5, 102.0, 104.2)])
        shadow._shadows = None                         # «перезапуск»
        shadow.watch('FIBO', signal(), 'предел портфеля', now_ms=START + 2 * H)
        assert shadow._load() == []


class TestShadowsOfTheOldRulesAreReplayed:

    def test_a_waiting_shadow_above_its_target_is_played_again(self):
        """
        Тень, заведённая по прежним правилам, переигрывается с начала: брокер
        отдаёт свечи с её start_ts, и она закрывается, как закрылась бы заявка.
        """
        import json
        legacy = [{'key': ['FIBO', 'LTCUSDT', 'LONG', 66.91, 65.930955], 'strategy': 'FIBO',
                   'pair': 'LTCUSDT', 'direction': 'LONG', 'gate': 'предел портфеля',
                   'detail': '', 'first_at': '2026-09-25T10:32:05Z', 'refusals': 24,
                   'start_ts': START, 'last_ts': START + 7 * H, 'last_close': 69.66,
                   'entry': 66.91, 'limit': 66.91, 'stop0': 65.930955, 'stop': 65.930955,
                   'targets': [69.47, 72.18], 'fractions': [0.5, 0.5], 'breakeven': False,
                   'cost_share_pct': 3.0, 'expiry_h': 48.0, 'max_hold_h': 0.0,
                   'filled': False, 'fill_hours': None, 'hit': 0, 'left': 1.0,
                   'realized': 0.0, 'best_r': 0.0, 'worst_r': 0.0, 'outcome': '',
                   'closed_hours': None}]
        with open(shadow.STATE_PATH, 'w', encoding='utf-8') as fh:
            json.dump(legacy, fh)
        shadow._shadows = None
        assert shadow.pairs() == {'LTCUSDT': START}, 'свечи нужны с начала тени'
        done = run([(1, 68.0, 67.2, 67.9), (3, 69.6, 67.8, 69.5)], pair='LTCUSDT')
        assert [s['outcome'] for s in done] == ['цель без входа']
        assert closed_rows()[0]['refusals'] == '24'


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
