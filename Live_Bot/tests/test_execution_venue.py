"""
Биржа торгового счёта (execution/venue.py) — реорганизация, этап 7, шаг 3.

Запросы собирает настоящий ccxt на сохранённых описаниях рынков
(tests/fixtures/venue_markets.json) — сеть не нужна: запрос перехватывается
до отправки. Проверяется ровно то, на чём боевой путь уже спотыкался:
  - стоп уходит ВМЕСТЕ с заявкой входа (позиция не бывает без стопа);
  - Bybit: стоп позиции переносится trading-stop, «не изменилось» — не
    ошибка; стоп-заявка на вход с направлением; хедж узнаётся по отказу и
    заявка повторяется; статус заявки — с acknowledged;
  - BingX: сторона позиции BOTH/LONG по режиму счёта, плечо со стороной,
    перенос стопа — новый STOP_MARKET reduce-only, прежний снимается после;
  - объём округляется вниз до шага, позиции биржи — нашими символами.
"""

import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import ccxt
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from execution.venue import Venue, VenueError  # noqa: E402

MARKETS = json.load(open(os.path.join(ROOT, 'tests', 'fixtures', 'venue_markets.json'), encoding='utf-8'))


class Sent(Exception):
    pass


def client(name, reply=None):
    """Клиент ccxt с рынками из файла; запросы копятся в .sent, ответ — reply
    (без него запрос прерывается после сборки)."""
    opts = {'defaultType': 'swap' if name == 'bingx' else 'linear'}
    if name == 'bybit':
        opts.update(enableUnifiedAccount=True, enableUnifiedMargin=False)
    c = getattr(ccxt, name)({'apiKey': 'k' * 18, 'secret': 's' * 36, 'options': opts})
    c.set_markets(list(MARKETS[name].values()))
    c.sent = []

    def fetch(url, method='GET', headers=None, body=None):
        c.sent.append({'method': method, 'url': url, 'body': body})
        if reply is None:
            raise Sent()
        return reply(url, body)
    c.fetch = fetch
    return c


def body(req):
    """Параметры запроса: JSON-тело (Bybit) или строка запроса (BingX)."""
    if req['body'] and req['body'].startswith('{'):
        return json.loads(req['body'])
    query = parse_qs(urlparse(req['url']).query)
    return {k: v[0] for k, v in query.items()}


def last(c):
    return body(c.sent[-1])


@pytest.fixture(autouse=True)
def fresh_symbols(monkeypatch):
    from data import exchange
    monkeypatch.setattr(exchange, '_symbol_cache', {})


# ── Bybit ───────────────────────────────────────────────────────────────────

class TestBybit:
    def venue(self, reply=None):
        v = Venue(client('bybit', reply))
        v.hedged = False
        return v

    def test_entry_carries_its_stop(self):
        v = self.venue()
        with pytest.raises(Sent):
            v.place_entry('BTCUSDT', True, 0.012, 62212.2, 61400.0)
        req = last(v.client)
        assert v.client.sent[-1]['url'].endswith('/v5/order/create')
        assert req['side'] == 'Buy' and req['orderType'] == 'Limit' and req['timeInForce'] == 'GTC'
        assert req['qty'] == '0.012' and req['price'] == '62212.2' and req['stopLoss'] == '61400'
        assert 'takeProfit' not in req          # тейки ставятся лимитами после налива

    def test_breakout_entry_has_a_direction(self):
        v = self.venue()
        with pytest.raises(Sent):
            v.place_entry('BTCUSDT', False, 0.012, 61000.0, 62000.0, kind='stop')
        req = last(v.client)
        assert req['orderType'] == 'Market' and req['triggerPrice'] == '61000'
        assert req['triggerDirection'] == 2 and req['stopLoss'] == '62000'

    def test_stop_moves_on_the_position(self):
        v = self.venue()
        with pytest.raises(VenueError):                 # ответа нет — стоп не подтверждён
            v.move_stop('BTCUSDT', True, 62212.157, 0.012)
        assert v.client.sent[-1]['url'].endswith('/v5/position/trading-stop')
        req = last(v.client)
        assert req == {'category': 'linear', 'symbol': 'BTCUSDT', 'stopLoss': '62212.2',
                       'slTriggerBy': 'LastPrice', 'positionIdx': 0}

    def test_stop_not_modified_is_fine(self):
        def reply(url, body):
            raise ccxt.ExchangeError('bybit {"retCode":34040,"retMsg":"not modified"}')
        assert self.venue(reply).move_stop('BTCUSDT', True, 62000.0, 0.012) is True

    def test_hedge_mode_is_learned_from_the_refusal(self):
        calls = []

        def reply(url, body):
            calls.append(json.loads(body))
            if len(calls) == 1:
                raise ccxt.BadRequest('bybit {"retCode":10001,"retMsg":"position idx not match position mode"}')
            return {'retCode': 0, 'retMsg': 'OK', 'result': {'orderId': 'abc', 'orderLinkId': ''},
                    'time': 1}
        v = self.venue(reply)
        assert v.place_entry('BTCUSDT', True, 0.012, 62000.0, 61400.0) == 'abc'
        assert 'positionIdx' not in calls[0] and calls[1]['positionIdx'] == 1
        assert v.hedged is True

    def test_order_status_is_acknowledged(self):
        v = self.venue()
        assert v.order('BTCUSDT', '123') is None       # все пути прерваны — статуса нет
        assert '/v5/order/realtime' in v.client.sent[0]['url']

    def test_take_profit_is_a_reduce_only_limit(self):
        v = self.venue()
        with pytest.raises(Sent):
            v.take_profit('BTCUSDT', True, 0.006, 64000.0)
        req = last(v.client)
        assert req['side'] == 'Sell' and req['orderType'] == 'Limit' and req['reduceOnly'] is True

    def test_amount_is_rounded_down(self):
        v = self.venue()
        assert v.amount('BTCUSDT', 0.12399) == 0.123
        assert v.amount('BTCUSDT', 0.0004) == 0.0          # меньше шага
        assert v.minimum('BTCUSDT')[0] == 0.001


# ── BingX ───────────────────────────────────────────────────────────────────

class TestBingx:
    def venue(self, hedged=False, reply=None):
        v = Venue(client('bingx', reply))
        v.hedged = hedged
        return v

    def test_one_way_entry_with_stop(self):
        v = self.venue()
        with pytest.raises(Sent):
            v.place_entry('BTCUSDT', True, 0.012, 62212.2, 61400.0)
        req = last(v.client)
        assert req['symbol'] == 'BTC-USDT' and req['positionSide'] == 'BOTH' and req['side'] == 'BUY'
        stop = json.loads(req['stopLoss'])
        assert stop['stopPrice'] == 61400 and stop['type'] == 'STOP_MARKET'

    def test_hedge_entry_and_close_sides(self):
        v = self.venue(hedged=True)
        with pytest.raises(Sent):
            v.place_entry('BTCUSDT', False, 0.012, 61000.0, 62000.0)
        assert last(v.client)['positionSide'] == 'SHORT'
        with pytest.raises(Sent):
            v.close_market('BTCUSDT', False, 0.012)
        req = last(v.client)
        assert req['side'] == 'BUY' and req['positionSide'] == 'SHORT' and 'reduceOnly' not in req

    def test_leverage_names_the_side(self):
        v = self.venue()
        with pytest.raises(VenueError):
            v.set_leverage('BTCUSDT', 10)
        req = last(v.client)
        assert req['side'] == 'BOTH' and req['leverage'] == '10'

    def test_stop_move_places_new_then_removes_old(self, monkeypatch):
        v = self.venue()
        order = {'code': 0, 'msg': '', 'data': {'order': {'orderId': '777', 'symbol': 'BTC-USDT', 'side': 'SELL',
                                                         'type': 'STOP_MARKET', 'positionSide': 'BOTH'}}}
        monkeypatch.setattr(v.client, 'fetch', lambda url, method='GET', headers=None, body=None:
                            v.client.sent.append({'method': method, 'url': url, 'body': body}) or order)
        cancelled = []
        monkeypatch.setattr(v.client, 'fetch_open_orders', lambda sym: [
            {'id': '777', 'type': 'stop_market', 'info': {'type': 'STOP_MARKET'}},
            {'id': '555', 'type': 'stop_market', 'info': {'type': 'STOP_MARKET'}},
            {'id': '666', 'type': 'limit', 'info': {'type': 'TAKE_PROFIT_MARKET'}},
            {'id': '888', 'type': 'limit', 'info': {'type': 'LIMIT'}}])
        monkeypatch.setattr(v.client, 'cancel_order', lambda oid, sym: cancelled.append(oid))
        assert v.move_stop('BTCUSDT', True, 62000.0, 0.012) is True
        req = body(v.client.sent[0])
        assert req['type'] == 'STOP_MARKET' and req['stopPrice'] == '62000' and req['reduceOnly'] == 'true'
        assert cancelled == ['555']                     # новый остался, тейки и лимиты не тронуты

    def test_min_cost(self):
        assert self.venue().minimum('BTCUSDT')[1] == 2.0


class TestPositions:
    def test_exchange_symbols_become_ours(self, monkeypatch):
        v = Venue(client('bingx'))
        monkeypatch.setattr(v.client, 'fetch_positions', lambda: [
            {'symbol': 'BTC/USDT:USDT', 'contracts': 0.012, 'contractSize': 1, 'side': 'long', 'entryPrice': 62000},
            {'symbol': 'ETH/USDT:USDT', 'contracts': 0, 'contractSize': 1, 'side': 'short'}])
        assert v.positions() == {'BTCUSDT': {'size': 0.012, 'long': True, 'entry': 62000.0}}

    def test_unknown_market_is_an_error(self):
        with pytest.raises(VenueError):
            Venue(client('bybit')).symbol('NOPEUSDT')
