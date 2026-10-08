"""
Биржа торгового счёта — тонкая обёртка ccxt для исполнения (реорганизация,
этап 7, шаг 3): заявка входа со стопом, её статус и отмена, позиция, перенос
стопа, тейки, закрытие по рынку, исполненные сделки, капитал.

Решений здесь нет — только «как сказать это бирже». Особенности проверены
сборкой запросов ccxt 4.5 (запрос собирается, но не отправляется):
  - символ: наш 'BTCUSDT' → бессрочный рынок биржи (exchange.market_symbol;
    у BingX это 'BTC-USDT');
  - стоп крепится к заявке входа (stopLoss): позиция не бывает без стопа
    даже тогда, когда бот выключен;
  - Bybit: стоп позиции переносится trading-stop (34040 «не изменилось» —
    не ошибка); статус заявки — fetch_order с acknowledged; режим позиций
    ccxt у Bybit не спрашивает — по умолчанию односторонний, хедж узнаётся по
    отказу «position idx» и запоминается;
  - BingX: режим позиций спрашивается (fetch_position_mode), в хедже ccxt
    сам ставит positionSide; стоп позиции — отдельный STOP_MARKET reduce-only
    с объёмом (прежний снимается ПОСЛЕ постановки нового); плечо — со
    стороной (BOTH, LONG, SHORT).
"""

from logger import log

NOT_MODIFIED = ('34040', 'not modified', '110043')


def norm(symbol):
    """Наш символ рынка: 'BTC/USDT:USDT', 'BTC-USDT' → 'BTCUSDT'."""
    return (symbol or '').replace('/', '').replace(':USDT', '').replace(':', '').replace('-', '').upper()


class VenueError(Exception):
    """Биржа отказала — текст для журнала и владельца."""


def _quiet(exc):
    """«Не изменилось» — не ошибка: уже стоит там, куда ставим."""
    text = str(exc).lower()
    return any(code in text for code in NOT_MODIFIED)


class Venue:
    def __init__(self, client):
        self.client = client
        self.id = getattr(client, 'id', '') or ''
        self.hedged = None          # режим позиций; None — ещё не узнан
        self._leverage = {}         # символ → выставленное плечо

    # ── Рынок ────────────────────────────────────────────────────────────────

    def symbol(self, pair):
        import exchange
        sym = exchange.market_symbol(pair, self.client)
        if not sym:
            raise VenueError(f'{pair}: на {self.id} такого рынка нет')
        return sym

    def market(self, pair):
        return self.client.market(self.symbol(pair))

    def amount(self, pair, size):
        """Объём, округлённый ВНИЗ до шага биржи; 0 — меньше шага."""
        try:
            return float(self.client.amount_to_precision(self.symbol(pair), size))
        except Exception:                          # noqa: BLE001
            return 0.0

    def minimum(self, pair):
        """(наименьший объём, наименьшая сумма заявки в USDT)."""
        limits = self.market(pair).get('limits') or {}
        amount = (limits.get('amount') or {}).get('min') or 0
        cost = (limits.get('cost') or {}).get('min') or 0
        return float(amount), float(cost)

    def max_leverage(self, pair):
        value = ((self.market(pair).get('limits') or {}).get('leverage') or {}).get('max')
        return float(value) if value else None

    # ── Счёт ─────────────────────────────────────────────────────────────────

    def balance(self):
        """Капитал счёта в USDT (баланс кошелька фьючерсов)."""
        data = self.client.fetch_balance()
        total = (data.get('total') or {}).get('USDT')
        if total is None:
            total = (data.get('USDT') or {}).get('total')
        if total is None:
            raise VenueError('биржа не отдала баланс USDT')
        return float(total)

    def _mode(self):
        if self.hedged is None:
            self.hedged = False
            if self.id == 'bingx':
                try:
                    self.hedged = bool((self.client.fetch_position_mode() or {}).get('hedged'))
                except Exception as exc:           # noqa: BLE001
                    log(f'   ⚠️ {self.id}: режим позиций не узнан ({exc}) — считаю односторонним')
        return self.hedged

    def _params(self, extra=None):
        out = dict(extra or {})
        if self._mode():
            out['hedged'] = True
        return out

    def set_leverage(self, pair, leverage):
        """Плечо по рынку — один раз на символ; «не изменилось» — не ошибка."""
        sym = self.symbol(pair)
        if self._leverage.get(sym) == leverage:
            return
        params = {}
        if self.id == 'bingx':
            params = {'side': 'BOTH'} if not self._mode() else {'side': 'LONG'}
        try:
            self.client.set_leverage(leverage, sym, params)
            if self.id == 'bingx' and self._mode():
                self.client.set_leverage(leverage, sym, {'side': 'SHORT'})
        except Exception as exc:                   # noqa: BLE001
            if not _quiet(exc):
                raise VenueError(f'плечо {leverage}× не выставлено: {exc}') from exc
        self._leverage[sym] = leverage

    # ── Заявки ───────────────────────────────────────────────────────────────

    def _create(self, pair, type_, side, amount, price=None, params=None):
        sym = self.symbol(pair)
        try:
            return self.client.create_order(sym, type_, side, amount, price, self._params(params))
        except Exception as exc:                   # noqa: BLE001
            # Bybit в режиме хеджа отвечает на заявку без стороны позиции
            # «position idx not match position mode»: запоминаем и повторяем.
            if self.id == 'bybit' and not self.hedged and 'position idx' in str(exc).lower():
                self.hedged = True
                return self.client.create_order(sym, type_, side, amount, price, self._params(params))
            raise

    def place_entry(self, pair, long_, amount, price, stop, kind='limit'):
        """
        Заявка входа со стопом, прикреплённым к ней.
          kind='limit'   лимит GTC по price;
          kind='stop'    вход по ходу движения: рыночный при пересечении price;
          kind='market'  по рынку сейчас.
        Возвращает id заявки.
        """
        side = 'buy' if long_ else 'sell'
        params = {'stopLoss': {'triggerPrice': float(stop)}}
        if kind == 'limit':
            params['timeInForce'] = 'GTC'
            order = self._create(pair, 'limit', side, amount, float(price), params)
        elif kind == 'stop':
            params['triggerPrice'] = float(price)
            if self.id == 'bybit':          # без направления Bybit заявку не примет
                params['triggerDirection'] = 'ascending' if long_ else 'descending'
            order = self._create(pair, 'market', side, amount, None, params)
        else:
            order = self._create(pair, 'market', side, amount, None, params)
        if not order or not order.get('id'):
            raise VenueError('биржа не вернула номер заявки')
        return str(order['id'])

    def take_profit(self, pair, long_, amount, price):
        """Тейк — лимит reduce-only по цели (мейкер, как в тесте). Возвращает id."""
        order = self._create(pair, 'limit', 'sell' if long_ else 'buy', amount, float(price),
                             {'reduceOnly': True})
        return str((order or {}).get('id') or '')

    def order(self, pair, order_id):
        """
        Статус заявки: {'status': open|closed|canceled|rejected|expired,
        'filled', 'average', 'amount'} или None — биржа не ответила.
        """
        sym = self.symbol(pair)
        # Bybit без acknowledged отвечает отказом «только последние 500 заявок»
        # (боевой исполнитель на этом терял наливы); другим он не нужен.
        extra = {'acknowledged': True} if self.id == 'bybit' else {}
        try:
            raw = self.client.fetch_order(order_id, sym, extra)
        except Exception:                          # noqa: BLE001
            try:
                raw = self.client.fetch_open_order(order_id, sym)
            except Exception:                      # noqa: BLE001
                try:
                    raw = self.client.fetch_closed_order(order_id, sym)
                except Exception as exc:           # noqa: BLE001
                    log(f'   ⚠️ {pair}: статус заявки {order_id} недоступен — {exc}')
                    return None
        if not raw:
            return None
        return {'status': str(raw.get('status') or 'open'),
                'filled': float(raw.get('filled') or 0),
                'average': float(raw.get('average') or raw.get('price') or 0) or None,
                'amount': float(raw.get('amount') or 0)}

    def cancel(self, pair, order_id):
        """Снять заявку. Уже исполненную или снятую биржа не найдёт — не ошибка."""
        try:
            self.client.cancel_order(order_id, self.symbol(pair))
            return True
        except Exception as exc:                   # noqa: BLE001
            log(f'   {pair}: заявка {order_id} не снята ({exc}) — проверю статус')
            return False

    def cancel_all(self, pair):
        """Снять все заявки по рынку (остатки тейков и стопов после выхода)."""
        try:
            self.client.cancel_all_orders(self.symbol(pair))
        except Exception as exc:                   # noqa: BLE001
            log(f'   {pair}: заявки по рынку не сняты — {exc}')

    # ── Позиции ──────────────────────────────────────────────────────────────

    def positions(self):
        """Открытые позиции счёта: наш символ → {'size', 'long', 'entry'}."""
        out = {}
        for pos in self.client.fetch_positions() or []:
            size = abs(float(pos.get('contracts') or 0)) * float(pos.get('contractSize') or 1)
            if not size:
                continue
            market = self.client.market(pos['symbol']) if pos.get('symbol') else {}
            pair = norm(market.get('id') or pos.get('symbol'))
            side = str(pos.get('side') or '').lower()
            out[pair] = {'size': size, 'long': side != 'short',
                         'entry': float(pos.get('entryPrice') or 0) or None}
        return out

    def move_stop(self, pair, long_, price, size):
        """Перенести стоп открытой позиции. True — стоит."""
        sym = self.symbol(pair)
        if self.id == 'bybit':
            index = 0 if not self._mode() else (1 if long_ else 2)
            try:
                self.client.privatePostV5PositionTradingStop({
                    'category': 'linear', 'symbol': self.client.market(sym)['id'],
                    'stopLoss': str(self.client.price_to_precision(sym, price)),
                    'slTriggerBy': 'LastPrice', 'positionIdx': index})
                return True
            except Exception as exc:               # noqa: BLE001
                if _quiet(exc):
                    return True
                raise VenueError(f'стоп не перенесён: {exc}') from exc
        # BingX и прочие: новый STOP_MARKET reduce-only с объёмом, затем снять
        # прежние стопы — позиция ни мгновения не остаётся без защиты.
        try:
            new = self._create(pair, 'market', 'sell' if long_ else 'buy', size, None,
                               {'stopLossPrice': float(price), 'reduceOnly': True})
        except Exception as exc:                   # noqa: BLE001
            raise VenueError(f'стоп не перенесён: {exc}') from exc
        new_id = str((new or {}).get('id') or '')
        try:
            for o in self.client.fetch_open_orders(sym) or []:
                kind = str((o.get('info') or {}).get('type') or o.get('type') or '').upper()
                if 'STOP' in kind and 'PROFIT' not in kind and str(o.get('id')) != new_id:
                    self.client.cancel_order(o['id'], sym)
        except Exception as exc:                   # noqa: BLE001
            log(f'   {pair}: прежний стоп не снят ({exc}) — сработает тот, что ближе')
        return True

    def close_market(self, pair, long_, size):
        """Закрыть остаток позиции по рынку (reduce-only)."""
        self._create(pair, 'market', 'sell' if long_ else 'buy', size, None, {'reduceOnly': True})

    def fills(self, pair, since_ms):
        """Исполненные сделки по рынку с момента since_ms:
        [{'ts', 'side', 'price', 'amount', 'fee'}] (комиссия — в USDT)."""
        out = []
        for t in self.client.fetch_my_trades(self.symbol(pair), int(since_ms)) or []:
            fee = t.get('fee') or {}
            cost = float(fee.get('cost') or 0) if (fee.get('currency') in (None, 'USDT')) else 0.0
            out.append({'ts': int(t.get('timestamp') or 0), 'side': str(t.get('side') or ''),
                        'price': float(t.get('price') or 0), 'amount': float(t.get('amount') or 0),
                        'fee': cost})
        return sorted(out, key=lambda x: x['ts'])
