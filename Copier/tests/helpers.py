"""Общие заготовки тестов: книга счёта бота, фальшивый Upscale, сборщик уведомлений."""

import asyncio
import json
import os
import uuid
from decimal import Decimal

from copier.upscale import to_fp9


def pending(pair='BTCUSDT', strategy='FIBO', direction='LONG', limit=100.0, stop=95.0,
            targets=(110.0,), fractions=(1.0,), placed_ts=1_000, stop_entry=False):
    """Заявка в книге счёта бота (/api/accounts/feed, format 1)."""
    return {'id': f'{pair}@{placed_ts}', 'pair': pair, 'strategy': strategy, 'direction': direction,
            'limit_price': limit, 'stop_loss': stop, 'targets': list(targets),
            'fractions': list(fractions), 'stop_entry': stop_entry,
            'entry_type': 'STOP' if stop_entry else 'LIMIT', 'placed_ts': placed_ts,
            'expires_ts': placed_ts + 3_600_000, 'invalidation': stop, 'cancel_beyond': None,
            'max_hold_hours': None}


def position(order, trade_id=1, entry=None, stop=None, tp_hit=0, remaining=1.0, ai_half=None,
             last_price=None):
    """Позиция в книге счёта бота."""
    return {'trade_id': trade_id, 'strategy': order['strategy'], 'pair': order['pair'],
            'direction': order['direction'], 'entry_price': entry or order['limit_price'],
            'stop_loss': stop or order['stop_loss'], 'initial_stop': order['stop_loss'],
            'targets': order['targets'], 'fractions': order['fractions'], 'tp_hit': tp_hit,
            'size': 20 * remaining, 'initial_size': 20, 'remaining': remaining,
            'breakeven_set': stop is not None and stop != order['stop_loss'], 'ai_half': ai_half,
            'opened_ts': order['placed_ts'] + 60_000, 'placed_ts': order['placed_ts'],
            'last_price': last_price or entry or order['limit_price'], 'last_ts': order['placed_ts']}


def closed_row(pos, reason='TP1', exit_price=110.0, t=2_000.0, pnl_r=2.0):
    return {'trade_id': pos['trade_id'], 'strategy': pos['strategy'], 'pair': pos['pair'],
            'direction': pos['direction'], 'exit_reason': reason, 'exit_price': exit_price,
            'closed_at': '2026-10-27T10:00:00+00:00', '_t': t, 'pnl_r': pnl_r, 'account': 'upscale'}


def book(pending_recs=(), position_recs=(), closed=(), status='active', phase=1, instructions=(),
         account='upscale', fmt=1):
    return {'format': fmt, 'account': account, 'at': '2026-10-27T10:00:00+00:00', 'ts': 0,
            'rules': {'name': 'Upscale', 'strategies': ['FIBO', 'SMC'], 'risk_pct': 1.0,
                      'max_slots': 4, 'max_same_direction': 3, 'daily_loss_pct': 5.0,
                      'max_drawdown_pct': 10.0, 'ai_control': True},
            'status': status, 'status_why': '', 'status_at': None, 'phase': phase,
            'phase_started_at': '2026-10-26T21:44:16+00:00', 'start_balance': 10000.0,
            'balance': 10000.0, 'equity': 10000.0, 'room': None,
            'pending': list(pending_recs), 'positions': list(position_recs),
            'closed': list(closed), 'instructions': list(instructions)}


class FakeBot:
    """Панель бота: отдаёт текущую книгу; closed — новее since_ts, как настоящая."""

    def __init__(self, data=None):
        self.data = data or book()
        self.down = False
        self.calls = []

    async def fetch(self, account, since_ts):
        self.calls.append((account, since_ts))
        if self.down:
            raise RuntimeError('Cannot connect to host 127.0.0.1:8787')
        out = dict(self.data)
        out['closed'] = [r for r in self.data.get('closed') or [] if float(r['_t']) * 1000 > since_ts]
        return out


def fp(x):
    return to_fp9(x)


def upscale_account(account_id='acc-1', balance=10_000, equity=None, kind='demo'):
    equity = balance if equity is None else equity
    return {'accountId': account_id, 'userId': 'user-1', 'type': kind, 'status': 'active', 'apiTrading': True,
            'currentPhase': 'active_evaluation', 'initialAccountBalance': fp(balance),
            # Как отдаёт Upscale (демо-счёт, 27.10.2026): правила — проценты обычным числом.
            'maxDailyDrawdown': '5', 'maxTotalDrawdown': '10',
            'profitTarget': '5', 'antifraudStatus': 'normal', 'tradingLockReason': None,
            'riskStatus': {'currentBalance': fp(balance), 'currentEquity': fp(equity),
                           'dayStartEquity': fp(balance), 'maxEquity': fp(balance),
                           'consistencyRuleRatio': None}}


def market(market_id, base, leverages=(5, 5, 2), closed=False):
    """Рынок в формате /v2/markets (демо 27.10.2026): плечо — settings.maxLeverageByAccount."""
    return {'id': market_id, 'config': {'baseAsset': base, 'quoteAsset': 'USD'},
            'settings': {'maxLeverageByAccount': {str(i): fp(v) for i, v in enumerate(leverages)},
                         'isClosed': closed, 'isPaused': False, 'isCloseOnly': False},
            'state': {'indexPrice': fp(100)}}


class FakeUpscale:
    """Счёт Upscale в памяти: ордера, позиции, журнал вызовов."""

    def __init__(self, accounts=None, markets=None, leverage_allowed=None):
        self.accounts_list = accounts or [upscale_account()]
        self.markets_list = markets or [market('m-btc', 'BTC'), market('m-eth', 'ETH')]
        self.leverage_allowed = leverage_allowed      # вид счёта: выше — invalid_leverage
        self.orders = {}
        self.positions = {}
        self.calls = []
        self.fail_next = None

    async def send(self, method, url, headers, body):
        path = url.split('api.upscale.trade', 1)[-1].split('?')[0]
        self.calls.append((method, path, body, headers.get('x-idempotency-key')))
        if self.fail_next:
            status, code = self.fail_next
            self.fail_next = None
            return status, {'statusCode': status, 'error': code, 'message': code}, {}
        if method == 'GET' and path == '/accounts/with-risk-status':
            return 200, self.accounts_list, {}
        if method == 'GET' and path == '/v2/markets':
            return 200, self.markets_list, {}
        if method == 'GET' and path.endswith('/active') and path.startswith('/orders/'):
            return 200, list(self.orders.values()), {}
        if method == 'GET' and path.endswith('/active') and path.startswith('/positions/'):
            return 200, list(self.positions.values()), {}
        if method == 'GET' and path.startswith('/positions/'):
            # Как настоящий (демо 27.10.2026): pnl до комиссий, fee отдельно, выход — exchanged*.
            return 200, {'status': 'closed', 'eventName': 'closePosition', 'pnl': fp(210), 'fee': fp(10),
                         'funding': '0', 'exchangedQuote': fp(2200), 'exchangedBase': fp(20)}, {}
        if method == 'POST' and path == '/orders':
            if self.leverage_allowed and body.get('leverage') and int(body['leverage']) > int(fp(self.leverage_allowed)):
                return 400, {'statusCode': 400, 'error': 'invalid_leverage', 'message': 'maxLeverage'}, {}
            oid = str(uuid.uuid4())
            order = dict(body, id=oid, market=body['marketId'], status='active')
            if body['type'] in ('limit', 'market'):
                self.orders[oid] = order
            elif body['type'] == 'take' and body.get('triggerPrice') == '0':
                pos = self.positions.get(body['positionId'])
                if pos:
                    left = int(pos['size']) - int(body['amount'])
                    if left <= 0:
                        self.positions.pop(body['positionId'], None)
                    else:
                        pos['size'] = str(left)
            else:
                self.orders[oid] = order
            return 200, order, {}
        if method == 'PATCH' and path.startswith('/orders/'):
            # Как настоящий (демо 27.10.2026): изменение = НОВЫЙ ордер, прежний canceled_by_update.
            oid = path.rsplit('/', 1)[-1]
            if oid not in self.orders:
                return 403, {'error': 'order_not_active', 'message': 'canceled_by_update'}, {}
            new_id = str(uuid.uuid4())
            self.orders[new_id] = dict(self.orders.pop(oid), id=new_id, triggerPrice=body['triggerPrice'])
            return 200, self.orders[new_id], {}
        if method == 'DELETE' and path.startswith('/orders/'):
            self.orders.pop(path.rsplit('/', 1)[-1], None)
            return 204, None, {}
        if method == 'POST' and path.endswith('/close-all'):
            self.orders.clear()
            self.positions.clear()
            return 200, {}, {}
        return 404, {'error': 'not_found'}, {}

    def fill(self, order_id, price=None):
        """Налить лимит: позиция + привязанный стоп как отдельный ордер."""
        order = self.orders.pop(order_id)
        pid = 'pos-' + order_id[:8]
        # Как настоящая позиция: entryPrice нет, есть notional и size.
        entry = Decimal(str(price)) if price else Decimal(order['triggerPrice']) / Decimal(10) ** 9
        size = Decimal(order['baseSize']) / Decimal(10) ** 9
        self.positions[pid] = {'idx': pid, 'market': order['marketId'], 'status': 'open',
                               'direction': order['direction'], 'size': order['baseSize'],
                               'notional': fp(entry * size)}
        if order.get('stopTriggerPrice'):
            sid = 'stop-' + order_id[:8]
            self.orders[sid] = {'id': sid, 'type': 'stop', 'positionId': [pid],
                                'market': order['marketId'], 'triggerPrice': order['stopTriggerPrice']}
        return pid


class Notes:
    def __init__(self):
        self.users, self.admins = [], []

    async def user(self, tg_id, text, buttons=None):
        self.users.append((tg_id, text))

    async def admin(self, text, buttons=None):
        self.admins.append(text)


def run(coro):
    return asyncio.run(coro)
