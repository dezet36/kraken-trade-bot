"""
Клиент публичного API Upscale (docs.upscale.trade/ru/developers).

- Авторизация: Authorization: Bearer usk_… — ключ участника, свой на каждый счёт.
- Числа — строки fp9 (значение × 10⁹). Считать только через Decimal.
- Запросы с телом несут x-idempotency-key (UUID v4). Повтор того же логического
  запроса — с ТЕМ ЖЕ ключом: сервер вернёт сохранённый ответ, второй ордер не
  появится. Поэтому ключ создаёт вызывающий и хранит до ответа (follower — в базе).
- Лимиты на ключ: 10 в секунду и 60 в минуту, отдельно чтение и запись;
  превышение — 429 с Retry-After. Держимся ниже: 8 и 50.

Что в документации не описано и подтверждается на демо (tools/probe.py):
стоп и тейк на лимитной заявке, несколько take на позицию, поиск и правка
привязанного стопа, имена полей шага цены и плеча в /v2/markets.
"""

import asyncio
import json
import time
import uuid
from collections import deque
from decimal import Decimal, ROUND_DOWN, InvalidOperation

from copier import config
from copier.log import log, scrub

FP9 = Decimal(10) ** 9


def to_fp9(value):
    """Число → строка fp9. Дробная часть меньше 1e-9 отбрасывается."""
    d = Decimal(str(value))
    return str(int((d * FP9).to_integral_value(rounding=ROUND_DOWN)))


def from_fp9(raw):
    """Строка (или массив из одной строки — так местами описана схема) fp9 → Decimal; пусто → None."""
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None or raw == '':
        return None
    try:
        return Decimal(str(raw)) / FP9
    except (InvalidOperation, ValueError):
        return None


def new_idem_key():
    return str(uuid.uuid4())


class UpscaleError(Exception):
    """Ответ с ошибкой. Ветвиться по code (поле error), не по тексту."""

    def __init__(self, status, code, message=''):
        super().__init__(f'{status} {code}: {message}')
        self.status = status
        self.code = code or ''
        self.message = message or ''

    @property
    def key_dead(self):
        """Ключ больше не работает — нужен новый от участника."""
        return self.status == 401 or self.code in ('api_trading_not_enabled', 'api_key_not_allowed',
                                                   'account_access_denied')

    @property
    def account_closed(self):
        return self.code in ('challenge_closed', 'account_phase_not_allowed',
                             'funded_limit_trading_locked')


class _Bucket:
    """Скользящие окна: не больше per_s за секунду и per_min за минуту."""

    def __init__(self, per_s=8, per_min=50):
        self.per_s, self.per_min = per_s, per_min
        self.stamps = deque()
        self.lock = asyncio.Lock()

    async def take(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.stamps and now - self.stamps[0] > 60:
                    self.stamps.popleft()
                last_s = sum(1 for t in self.stamps if now - t <= 1)
                if len(self.stamps) < self.per_min and last_s < self.per_s:
                    self.stamps.append(now)
                    return
                if len(self.stamps) >= self.per_min:
                    wait = 60 - (now - self.stamps[0]) + 0.05
                else:
                    wait = 0.2
                await asyncio.sleep(max(wait, 0.05))


class UpscaleClient:
    """
    Один клиент — один ключ. send — подменяемая отправка (тесты без сети):
    async send(method, url, headers, body) -> (status, payload | None, headers).
    """

    RETRIES = 3

    def __init__(self, api_key, base_url=None, send=None, session=None):
        self._key = api_key
        self.base = (base_url or config.UPSCALE_URL).rstrip('/')
        self._send_fn = send
        self._session = session
        self._read = _Bucket()
        self._write = _Bucket()

    # ── транспорт ──────────────────────────────────────────────────────────
    async def _send(self, method, url, headers, body):
        if self._send_fn is not None:
            return await self._send_fn(method, url, headers, body)
        import aiohttp
        own = self._session is None
        session = self._session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            async with session.request(method, url, headers=headers,
                                       data=None if body is None else json.dumps(body)) as resp:
                text = await resp.text()
                try:
                    payload = json.loads(text) if text else None
                except ValueError:
                    payload = {'message': text[:300]}
                return resp.status, payload, dict(resp.headers)
        finally:
            if own:
                await session.close()

    async def call(self, method, path, body=None, params=None, idem_key=None):
        """Запрос с повторами: 429 — ждём Retry-After, 409 in_flight и сеть — тот же ключ идемпотентности."""
        url = self.base + path
        if params:
            url += '?' + '&'.join(f'{k}={v}' for k, v in params.items() if v is not None)
        headers = {'Authorization': f'Bearer {self._key}', 'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            headers['x-idempotency-key'] = idem_key or new_idem_key()
        bucket = self._read if method == 'GET' else self._write
        last_error = None
        for attempt in range(self.RETRIES + 1):
            await bucket.take()
            try:
                status, payload, resp_headers = await self._send(method, url, headers, body)
            except Exception as exc:                   # noqa: BLE001 — сеть: повтор с тем же ключом
                last_error = UpscaleError(0, 'network', scrub(exc))
                await asyncio.sleep(1 + attempt)
                continue
            if 200 <= status < 300:
                return payload
            payload = payload if isinstance(payload, dict) else {}
            err = UpscaleError(status, payload.get('error'), scrub(payload.get('message', '')))
            if status == 429 and attempt < self.RETRIES:
                retry = _retry_after(resp_headers, payload)
                log(f'upscale: 429 {path}, жду {retry:.1f} с')
                await asyncio.sleep(retry)
                last_error = err
                continue
            if status == 409 and err.code in ('idempotency_key_in_flight', 'account_not_loaded',
                                              'market_price_stale') and attempt < self.RETRIES:
                await asyncio.sleep(1 + attempt)
                last_error = err
                continue
            if status >= 500 and attempt < self.RETRIES:
                await asyncio.sleep(2 + attempt)
                last_error = err
                continue
            raise err
        raise last_error or UpscaleError(0, 'unknown')

    # ── аккаунты ───────────────────────────────────────────────────────────
    async def accounts(self):
        """Счета ключа со статусом риска (только с включённой API-торговлей)."""
        return await self.call('GET', '/accounts/with-risk-status') or []

    async def risk_status(self, account_id):
        return await self.call('GET', f'/accounts/{account_id}/risk-status')

    async def stats(self, account_id):
        return await self.call('GET', f'/accounts/{account_id}/stats')

    async def markets(self, account_id=None):
        return await self.call('GET', '/v2/markets', params={'accountId': account_id}) or []

    # ── ордера и позиции ───────────────────────────────────────────────────
    async def active_orders(self, account_id):
        return await self.call('GET', f'/orders/{account_id}/active') or []

    async def active_positions(self, account_id):
        return await self.call('GET', f'/positions/{account_id}/active') or []

    async def position_history(self, account_id, limit=50, offset=0):
        return await self.call('GET', f'/positions/{account_id}/portfolio/history',
                               params={'limit': limit, 'offset': offset}) or []

    async def position(self, position_id):
        return await self.call('GET', f'/positions/{position_id}')

    async def order_history(self, account_id, asset, limit=50):
        """Ордера по тикеру ({'data': [...], 'totalCount'}); ордера по id API не отдаёт."""
        return await self.call('GET', f'/orders/{account_id}/{asset}/history',
                               params={'limit': limit}) or {}

    async def create_order(self, body, idem_key):
        return await self.call('POST', '/orders', body=body, idem_key=idem_key)

    async def update_trigger(self, order_id, trigger_price, idem_key):
        return await self.call('PATCH', f'/orders/{order_id}',
                               body={'triggerPrice': to_fp9(trigger_price)}, idem_key=idem_key)

    async def cancel_order(self, order_id):
        return await self.call('DELETE', f'/orders/{order_id}')

    async def close_all(self, account_id, idem_key=None):
        """Закрыть все позиции и снять все ордера счёта."""
        return await self.call('POST', f'/accounts/{account_id}/close-all', body={},
                               idem_key=idem_key)


def _retry_after(headers, payload):
    for key in ('Retry-After', 'retry-after'):
        if headers and key in headers:
            try:
                return max(float(headers[key]), 0.5)
            except (TypeError, ValueError):
                pass
    return 2.0


# ── тела ордеров ───────────────────────────────────────────────────────────

def entry_order(account_id, market_id, direction, limit_price, base_size, leverage,
                reserve, stop=None, take=None, kind='limit'):
    """
    Ордер на вход: размер в базовом активе (sizeMode=base), стоп и тейк — на нём же.

    amount в схеме обязателен и для ордера на увеличение означает резерв в
    котируемой валюте: из него на исполнении берутся маржа по цене исполнения,
    комиссия и спред (демо 27.10.2026), поэтому передаётся маржа с запасом
    (sizing.Decision.reserve), а не ровно номинал/плечо.
    """
    body = {
        'accountId': account_id,
        'marketId': market_id,
        'type': kind,
        'direction': direction.lower(),
        'sizeMode': 'base',
        'baseSize': to_fp9(base_size),
        'amount': to_fp9(reserve),
        'leverage': to_fp9(leverage),
    }
    if kind != 'market':
        body['triggerPrice'] = to_fp9(limit_price)
    if stop is not None:
        body['stopTriggerPrice'] = to_fp9(stop)
    if take is not None:
        body['takeTriggerPrice'] = to_fp9(take)
    return body


def close_order(account_id, market_id, direction, position_id, base_amount, trigger=None,
                kind='take'):
    """Закрытие части или всей позиции: take по цене (trigger) или сейчас (trigger=None → "0")."""
    return {
        'accountId': account_id,
        'marketId': market_id,
        'type': kind,
        'direction': direction.lower(),
        'positionId': position_id,
        'amount': to_fp9(base_amount),
        'triggerPrice': '0' if trigger is None else to_fp9(trigger),
    }
