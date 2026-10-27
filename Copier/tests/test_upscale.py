from decimal import Decimal

import pytest

from copier import upscale
from copier.log import scrub
from tests.helpers import run


def test_fp9_roundtrip():
    assert upscale.to_fp9(1) == '1000000000'
    assert upscale.to_fp9('0.05') == '50000000'
    assert upscale.from_fp9('10000000000') == Decimal(10)
    assert upscale.from_fp9(['300000000']) == Decimal('0.3')
    assert upscale.from_fp9(None) is None


def test_scrub_hides_keys():
    text = scrub('Authorization: Bearer usk_abcDEF123 and usk_zzz')
    assert 'abcDEF' not in text and 'zzz' not in text


def test_retry_keeps_idempotency_key(monkeypatch):
    seen = []
    real_sleep = upscale.asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(upscale.asyncio, 'sleep', fast_sleep)

    async def send(method, url, headers, body):
        seen.append(headers.get('x-idempotency-key'))
        if len(seen) == 1:
            return 429, {'error': 'api_key_rate_limit_exceeded'}, {'Retry-After': '0.01'}
        if len(seen) == 2:
            return 409, {'error': 'idempotency_key_in_flight'}, {}
        return 200, {'id': 'o1'}, {}

    client = upscale.UpscaleClient('usk_test', send=send)
    resp = run(client.create_order({'x': 1}, 'idem-1'))
    assert resp == {'id': 'o1'}
    assert seen == ['idem-1', 'idem-1', 'idem-1']


def test_error_code_branching():
    async def send(method, url, headers, body):
        return 403, {'statusCode': 403, 'error': 'api_trading_not_enabled', 'message': 'x'}, {}

    client = upscale.UpscaleClient('usk_test', send=send)
    with pytest.raises(upscale.UpscaleError) as info:
        run(client.accounts())
    assert info.value.key_dead and info.value.code == 'api_trading_not_enabled'


def test_entry_order_body():
    body = upscale.entry_order('a', 'm', 'LONG', Decimal('100.5'), Decimal('2'), Decimal(5),
                               Decimal('40.2'), stop=Decimal(95), take=Decimal(110))
    assert body['direction'] == 'long' and body['type'] == 'limit'
    assert body['triggerPrice'] == '100500000000'
    assert body['baseSize'] == '2000000000' and body['sizeMode'] == 'base'
    assert body['stopTriggerPrice'] == '95000000000'
