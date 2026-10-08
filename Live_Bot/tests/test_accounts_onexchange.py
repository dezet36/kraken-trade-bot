"""
Биржевые торговые счета (accounts/onexchange.py) — реорганизация, этап 7, шаг 3.

Биржа подменена поддельной (заявки, позиции и сделки — в памяти; налив, тейк
и стоп «исполняет» проверка). Проверяется:
  - сетап → заявка на бирже сразу со стопом, объём — вниз до шага биржи,
    плечо такое, что ликвидация дальше стопа; меньше минимума — отказ;
  - реальные деньги не торгуют, пока не разрешены (сначала демо);
  - налив — тейки лимитами reduce-only по долям; тейк исполнился — стоп в
    безубыток на бирже; стоп сработал — строка журнала по сделкам биржи;
  - снятие заявки по сроку и слому сетапа, налившаяся часть — позиция;
  - срок удержания и безубыток от уровня — действием на бирже;
  - пределы счёта: закрыть всё и снять заявки на бирже;
  - сбои биржи: в журнал, владельцу — на третий подряд;
  - действия владельца, новый этап, панель;
  - фасад раздаёт сетап и пропу, и бирже.
"""

import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from accounts import books, live, onexchange, trading  # noqa: E402

BAR = books.BAR_MS
T0 = 300_000 * 5_866_667
H = 3_600_000


class Venue:
    """Биржа в памяти. Действия самой биржи (налив, тейк, стоп) делает проверка."""

    def __init__(self, cash=10_000.0):
        self.id = 'bybit'
        self.cash = cash
        self.orders = {}
        self.held = {}
        self.trades = []
        self.calls = []
        self.leverage = {}
        self.stops = {}
        self.step, self.least_amount, self.least_cost = 0.001, 0.001, 5.0
        self.fail = None
        self._n = 0

    def _id(self):
        self._n += 1
        return f'o{self._n}'

    def _check(self):
        if self.fail:
            raise self.fail

    # то, что зовёт исполнитель
    def amount(self, pair, size):
        return round(math.floor(size / self.step + 1e-9) * self.step, 9)

    def minimum(self, pair):
        return self.least_amount, self.least_cost

    def max_leverage(self, pair):
        return 100.0

    def balance(self):
        self._check()
        return self.cash

    def set_leverage(self, pair, leverage):
        self.leverage[pair] = leverage

    def place_entry(self, pair, long_, amount, price, stop, kind='limit'):
        self._check()
        oid = self._id()
        self.orders[oid] = {'pair': pair, 'long': long_, 'amount': amount, 'price': price, 'kind': kind,
                            'status': 'open', 'filled': 0.0, 'average': None, 'reduce': False}
        self.stops[pair] = stop
        self.calls.append(('entry', pair, long_, amount, price, stop, kind))
        return oid

    def take_profit(self, pair, long_, amount, price):
        oid = self._id()
        self.orders[oid] = {'pair': pair, 'long': long_, 'amount': amount, 'price': price, 'kind': 'tp',
                            'status': 'open', 'filled': 0.0, 'average': None, 'reduce': True}
        self.calls.append(('tp', pair, amount, price))
        return oid

    def order(self, pair, oid):
        o = self.orders[oid]
        return {'status': o['status'], 'filled': o['filled'], 'average': o['average'], 'amount': o['amount']}

    def cancel(self, pair, oid):
        self.calls.append(('cancel', pair, oid))
        if self.orders[oid]['status'] == 'open':
            self.orders[oid]['status'] = 'canceled'
        return True

    def cancel_all(self, pair):
        self.calls.append(('cancel_all', pair))
        for o in self.orders.values():
            if o['pair'] == pair and o['status'] == 'open':
                o['status'] = 'canceled'

    def positions(self):
        self._check()
        return {pair: dict(x) for pair, x in self.held.items() if x['size'] > 0}

    def move_stop(self, pair, long_, price, size):
        self.calls.append(('stop', pair, price))
        self.stops[pair] = price
        return True

    def close_market(self, pair, long_, size):
        self.calls.append(('close', pair, size))
        self._reduce(pair, size, self.last.get(pair, self.held[pair]['entry']), T0 + 50 * H)

    def fills(self, pair, since):
        return [t for t in self.trades if t['pair'] == pair and t['ts'] >= since]

    # то, что делает биржа
    last = {}

    def fill(self, oid, price=None, amount=None, ts=T0 + BAR):
        o = self.orders[oid]
        price = price if price is not None else o['price']
        amount = amount if amount is not None else o['amount']
        o['filled'], o['average'] = amount, price
        o['status'] = 'closed' if amount >= o['amount'] else 'open'
        self.held[o['pair']] = {'size': amount, 'long': o['long'], 'entry': price}
        self.trades.append({'pair': o['pair'], 'ts': ts, 'side': 'buy' if o['long'] else 'sell',
                            'price': price, 'amount': amount, 'fee': amount * price * 0.0002})
        self.cash -= amount * price * 0.0002

    def _reduce(self, pair, amount, price, ts):
        pos = self.held[pair]
        sign = 1 if pos['long'] else -1
        fee = amount * price * 0.00055
        self.cash += sign * (price - pos['entry']) * amount - fee
        pos['size'] = round(pos['size'] - amount, 9)
        self.trades.append({'pair': pair, 'ts': ts, 'side': 'sell' if pos['long'] else 'buy',
                            'price': price, 'amount': amount, 'fee': fee})

    def take(self, oid, ts=T0 + 3 * BAR):
        o = self.orders[oid]
        o['status'], o['filled'], o['average'] = 'closed', o['amount'], o['price']
        self._reduce(o['pair'], o['amount'], o['price'], ts)

    def stop_out(self, pair, ts=T0 + 4 * BAR):
        self._reduce(pair, self.held[pair]['size'], self.stops[pair], ts)


class Candles:
    def __init__(self, bars=None):
        self.bars = bars or {}

    def fetch_ohlcv(self, pair, timeframe, since=None, limit=500):
        return [b for b in self.bars.get(pair, []) if b[0] >= (since or 0)][:limit]

    def fetch_funding_rate(self, pair):
        return {'fundingRate': 0.0}


def bar(k, low, high, close=None):
    close = close if close is not None else (low + high) / 2
    return [T0 + k * BAR, close, high, low, close, 1.0]


def a_setup(pair='BTCUSDT', side='LONG', entry=100.0, stop=98.0, targets=(106.0,), fractions=(1.0,), **params):
    prm = {'entry': entry, 'stop_loss': stop, 'rr': 3.0, 'tp_targets': list(targets),
           'tp_fractions': list(fractions), 'breakeven_after_tp': False}
    prm.update(params)
    return {'trading_pair': pair, 'setup': {'type': side}, 'params': prm, 'trigger': {'entry_type': 'LIMIT'}}


@pytest.fixture(autouse=True)
def profile(monkeypatch):
    import strategy_profile
    values = {'limit_offset_pct': 0.0, 'cost_limit_pct': 0.0, 'expiry_hours': 24.0, 'cooldown_hours': 12.0,
              'max_hold_hours': 0.0, 'drops_at_target': True, 'fills_through_market': False}
    for key, value in values.items():
        monkeypatch.setattr(strategy_profile, key, lambda strategy, v=value: v)


@pytest.fixture
def venue(monkeypatch):
    v = Venue()
    Venue.last = {}
    monkeypatch.setattr(onexchange, '_venue', lambda code, rules: v if onexchange.tradable(rules) else None)
    return v


@pytest.fixture
def sent():
    out = []
    books.notify_with(lambda who, item: out.append(item))
    yield out
    books.notify_with(None)


def account(**rules):
    base = {'name': 'Bybit демо', 'kind': 'exchange', 'exchange': 'bybit', 'mode': 'demo',
            'strategies': ['SMCS'], 'risk_pct': 1}
    base.update(rules)
    enabled = base.pop('enabled', True)
    code, _ = live.save(base)
    live.set_keys(code, 'key', 'secret')
    live.save({'id': code, 'enabled': enabled})
    return code


def book(code):
    return onexchange.report(code, live.get(code), now_ms=T0)


def kinds(sent):
    return [item['kind'] for item in sent]


# ── Заявка на бирже ─────────────────────────────────────────────────────────

class TestPlacement:
    def test_entry_goes_with_its_stop(self, venue, sent):
        code = account()
        assert onexchange.offer('SMCS', a_setup(), now_ms=T0) == [(code, None)]
        assert venue.calls[0] == ('entry', 'BTCUSDT', True, 50.0, 100.0, 98.0, 'limit')
        assert venue.leverage['BTCUSDT'] == 20                  # стоп 2% — ликвидация дальше
        b = book(code)
        assert b['pending'][0]['risk'] == 100.0 and b['start'] == 10_000
        assert kinds(sent) == ['order'] and sent[0]['action'] is False

    def test_size_is_rounded_down_and_risk_follows(self, venue):
        venue.step = 0.7
        code = account()
        onexchange.offer('SMCS', a_setup(), now_ms=T0)
        assert venue.calls[0][3] == pytest.approx(49.7)
        assert book(code)['pending'][0]['risk'] == pytest.approx(99.4)

    def test_below_exchange_minimum_is_refused(self, venue):
        venue.least_cost = 1_000_000
        code = account()
        why = onexchange.offer('SMCS', a_setup(), now_ms=T0)[0][1]
        assert why.startswith('меньше минимума биржи') and venue.calls == []
        assert book(code)['counts']['refused'] == {'меньше минимума биржи': 1}

    def test_wide_stop_lowers_leverage(self, venue):
        account()
        onexchange.offer('SMCS', a_setup(stop=90.0), now_ms=T0)    # стоп 10% → плечо ≤ 8
        assert venue.leverage['BTCUSDT'] == 8

    def test_real_money_waits_for_the_demo(self, venue, monkeypatch):
        code = account(mode='live')
        assert not onexchange.wants('SMCS')
        assert onexchange.offer('SMCS', a_setup(), now_ms=T0) == [] and venue.calls == []
        monkeypatch.setattr(onexchange.books.cfg(), 'EXCHANGE_LIVE_ENABLED', True, raising=False)
        assert onexchange.offer('SMCS', a_setup(), now_ms=T0) == [(code, None)]

    def test_exchange_refusal_is_a_reason_not_a_crash(self, venue):
        venue.fail = RuntimeError('insufficient balance')
        code = account()
        why = onexchange.offer('SMCS', a_setup(), now_ms=T0)[0][1]
        assert why.startswith('биржа:') and code not in books.state()


# ── Ведение ─────────────────────────────────────────────────────────────────

def opened(venue, code, sent=None, **setup):
    onexchange.offer('SMCS', a_setup(**setup), now_ms=T0)
    oid = book(code)['pending'][0] and books.state()[code]['pending']['BTCUSDT']['order_id']
    venue.fill(oid)
    onexchange.update(Candles(), now_ms=T0 + 2 * BAR)
    return oid


class TestLifecycle:
    def test_fill_places_takes_by_shares(self, venue, sent):
        code = account()
        opened(venue, code, targets=(104.0, 108.0), fractions=(0.5, 0.5))
        assert [c for c in venue.calls if c[0] == 'tp'] == [('tp', 'BTCUSDT', 25.0, 104.0), ('tp', 'BTCUSDT', 25.0, 108.0)]
        b = book(code)
        assert not b['pending'] and b['positions'][0]['size'] == 50.0
        assert kinds(sent) == ['order', 'fill']

    def test_first_take_moves_the_stop_to_entry(self, venue, sent):
        code = account()
        opened(venue, code, targets=(104.0, 108.0), fractions=(0.5, 0.5), breakeven_after_tp=True)
        tp1 = next(oid for oid, o in venue.orders.items() if o['kind'] == 'tp')
        venue.take(tp1)
        onexchange.update(Candles(), now_ms=T0 + 5 * BAR)
        assert ('stop', 'BTCUSDT', 100.0) in venue.calls
        b = book(code)
        assert b['positions'][0]['size'] == 25.0 and b['positions'][0]['breakeven'] is True
        assert kinds(sent)[-2:] == ['target', 'breakeven']

    def test_stop_out_is_journaled_from_exchange_trades(self, venue, sent):
        code = account()
        opened(venue, code)
        venue.stop_out('BTCUSDT')
        onexchange.update(Candles(), now_ms=T0 + 6 * BAR)
        row = books.read_journal()[-1]
        assert row['account'] == code and row['exit_reason'] == 'SL' and row['outcome'] == 'минус'
        assert row['exit_price'] == 98.0 and row['estimated'] is False
        assert row['pnl_usd'] == pytest.approx(-100 - 50 * 100 * 0.0002 - 50 * 98 * 0.00055)
        assert row['balance_after'] == pytest.approx(venue.cash, abs=0.01)
        assert ('cancel_all', 'BTCUSDT') in venue.calls
        assert kinds(sent)[-1] == 'exit' and not book(code)['positions']

    def test_take_closes_with_profit(self, venue):
        code = account()
        opened(venue, code)
        tp = next(oid for oid, o in venue.orders.items() if o['kind'] == 'tp')
        venue.take(tp)
        onexchange.update(Candles(), now_ms=T0 + 6 * BAR)
        row = books.read_journal()[-1]
        assert row['exit_reason'] == 'TP1' and row['pnl_r'] > 2.9 and book(code)['quality']['wins'] == 1

    def test_expired_order_is_cancelled(self, venue, sent, monkeypatch):
        import strategy_profile
        monkeypatch.setattr(strategy_profile, 'expiry_hours', lambda s: 1.0)
        code = account()
        onexchange.offer('SMCS', a_setup(), now_ms=T0)
        onexchange.update(Candles(), now_ms=T0 + 2 * H)
        assert any(c[0] == 'cancel' for c in venue.calls)
        assert book(code)['counts']['dropped'] == {'срок заявки вышел': 1}
        assert kinds(sent)[-1] == 'cancel' and sent[-1]['action'] is False

    def test_broken_setup_is_cancelled(self, venue):
        code = account()
        onexchange.offer('SMCS', a_setup(), now_ms=T0)
        onexchange.update(Candles({'BTCUSDT': [bar(1, 97.0, 99.0)]}), now_ms=T0 + 3 * BAR)
        assert book(code)['counts']['dropped'] == {'сетап разрушен': 1}

    def test_part_filled_before_the_cancel_is_a_position(self, venue, monkeypatch):
        import strategy_profile
        monkeypatch.setattr(strategy_profile, 'expiry_hours', lambda s: 1.0)
        code = account()
        onexchange.offer('SMCS', a_setup(), now_ms=T0)
        oid = books.state()[code]['pending']['BTCUSDT']['order_id']
        venue.fill(oid, amount=10.0)
        onexchange.update(Candles(), now_ms=T0 + 2 * H)
        b = book(code)
        assert not b['pending'] and b['positions'][0]['size'] == 10.0

    def test_hold_limit_closes_on_the_exchange(self, venue, sent, monkeypatch):
        import strategy_profile
        monkeypatch.setattr(strategy_profile, 'max_hold_hours', lambda s: 1.0)
        code = account()
        opened(venue, code)
        Venue.last = {'BTCUSDT': 101.0}
        bars = [bar(k, 100.5, 101.5, close=101.0) for k in range(3, 20)]
        onexchange.update(Candles({'BTCUSDT': bars}), now_ms=T0 + 21 * BAR)
        assert ('close', 'BTCUSDT', 50.0) in venue.calls
        onexchange.update(Candles(), now_ms=T0 + 22 * BAR)
        assert books.read_journal()[-1]['exit_reason'] == 'TIME'
        assert 'close' in kinds(sent)

    def test_level_breakeven_moves_the_stop(self, venue):
        code = account()
        opened(venue, code, be_level=103.0, breakeven_after_tp=True)
        onexchange.update(Candles({'BTCUSDT': [bar(3, 101.0, 103.5)]}), now_ms=T0 + 5 * BAR)
        stop = [c for c in venue.calls if c[0] == 'stop'][-1][2]
        assert stop >= 100.0 and book(code)['positions'][0]['breakeven'] is True

    def test_limits_close_everything_on_the_exchange(self, venue, sent):
        code = account()
        opened(venue, code)
        onexchange.offer('SMCS', a_setup(pair='ETHUSDT', entry=50.0, stop=49.0, targets=(53.0,)), now_ms=T0)
        live.save({'id': code, 'max_drawdown_pct': 0.5})
        Venue.last = {'BTCUSDT': 98.7}
        onexchange.update(Candles({'BTCUSDT': [bar(3, 98.6, 99.2, close=98.7)]}), now_ms=T0 + 5 * BAR)
        assert ('close', 'BTCUSDT', 50.0) in venue.calls
        assert any(c[0] == 'cancel' for c in venue.calls)
        b = book(code)
        assert b['status'] == books.FAILED and not b['pending']
        assert kinds(sent)[-1] == 'rules'
        onexchange.update(Candles(), now_ms=T0 + 6 * BAR)
        assert books.read_journal()[-1]['exit_reason'] == 'RULES'
        assert not onexchange.wants('SMCS')


# ── Сбои, владелец, панель ──────────────────────────────────────────────────

class TestOwnerAndFaults:
    def test_failures_alert_on_the_third(self, venue, sent):
        code = account()
        onexchange.offer('SMCS', a_setup(), now_ms=T0)
        venue.fail = RuntimeError('timeout')
        for _ in range(4):
            onexchange.update(Candles(), now_ms=T0 + 2 * BAR)
        assert kinds(sent).count('error') == 1
        venue.fail = None
        onexchange.update(Candles(), now_ms=T0 + 3 * BAR)
        assert onexchange._fails.get(code) is None

    def test_owner_cancels_and_closes_on_the_exchange(self, venue):
        code = account()
        opened(venue, code)
        onexchange.offer('SMCS', a_setup(pair='ETHUSDT', entry=50.0, stop=49.0, targets=(53.0,)), now_ms=T0)
        assert 'снята на бирже' in onexchange.act(code, 'cancel', pair='ETHUSDT', now_ms=T0 + 3 * BAR)
        Venue.last = {'BTCUSDT': 101.0}
        assert 'отправлено' in onexchange.act(code, 'close', pair='BTCUSDT', now_ms=T0 + 3 * BAR)
        onexchange.update(Candles(), now_ms=T0 + 4 * BAR)
        assert books.read_journal()[-1]['exit_reason'] == 'MANUAL'
        b = book(code)
        assert not b['positions'] and not b['pending'] and b['counts']['dropped'] == {'снята владельцем': 1}

    def test_new_phase_starts_from_exchange_capital(self, venue):
        code = account()
        opened(venue, code)
        with pytest.raises(ValueError):
            onexchange.act(code, 'new_phase', now_ms=T0 + 3 * BAR)
        venue.stop_out('BTCUSDT')
        onexchange.update(Candles(), now_ms=T0 + 6 * BAR)
        venue.cash = 12_345.0
        onexchange.act(code, 'new_phase', now_ms=T0 + 7 * BAR)
        b = book(code)
        assert b['phase'] == 2 and b['start'] == 12_345.0

    def test_report_before_the_first_trade_has_no_money(self, venue):
        code = account()
        b = book(code)
        assert b['started'] is False and b['equity'] is None and b['tradable'] is True

    def test_facade_hands_the_setup_to_both_kinds(self, venue):
        prop, _ = live.save({'name': 'Проп', 'kind': 'manual', 'strategies': ['SMCS'], 'enabled': True})
        code = account()
        assert trading.wants('SMCS')
        got = dict(trading.offer('SMCS', a_setup()))
        assert got == {prop: None, code: None}
        assert trading.report(code, live.get(code))['pending'] and trading.report(prop, live.get(prop))['pending']
