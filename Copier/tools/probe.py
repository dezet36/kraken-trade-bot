"""
Проверка API Upscale на ДЕМО-счёте: то, чего нет в документации.

  python tools/probe.py            — только чтение + заявка далеко от рынка (не нальётся) и её снятие
  python tools/probe.py --fill     — ещё и крошечная позиция по рынку: тейки, перенос стопа, закрытие

Ключ: файл Copier/.probe_key (одна строка usk_…) или переменная UPSCALE_API_KEY.
Работает ТОЛЬКО с демо-счётом: реальный — отказ. Все ответы сервера пишутся в
Copier/probe_results/<время>.json (в git не попадает) — по ним правится клиент.

Что выясняем:
  1. поля /accounts/with-risk-status (проценты лимитов — доля или проценты);
  2. поля рынка в /v2/markets: шаг цены, шаг размера, предел плеча;
  3. лимит со stopTriggerPrice/takeTriggerPrice и sizeMode=base: принят ли, как
     сервер понимает amount при base;
  4. PATCH /orders/{id} — меняет ли цену лимита;
  5. (--fill) как выглядит позиция, привязанный стоп (type, positionId,
     parentOrderId), несколько take на одну позицию, PATCH стопа, закрытие take
     triggerPrice=0, поля GET /positions/{id} после закрытия.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('COPIER_DATA_DIR', os.path.join(ROOT, 'probe_results'))

from copier import markets as mk                          # noqa: E402
from copier.follower import pick, status_from_account     # noqa: E402
from copier.upscale import (UpscaleClient, UpscaleError, close_order, entry_order,  # noqa: E402
                            from_fp9, new_idem_key)

LOG = []


def note(step, data):
    LOG.append({'step': step, 'at': datetime.now(timezone.utc).isoformat(), 'data': data})
    text = json.dumps(data, ensure_ascii=False, default=str)
    print(f'\n== {step}\n{text[:1500]}')


async def call(step, coro):
    try:
        resp = await coro
        note(step, resp)
        return resp
    except UpscaleError as err:
        note(step, {'error': err.code, 'status': err.status, 'message': err.message})
        return None


async def bybit_price(symbol):
    """Цена с публичного API Bybit — если Upscale её в ответах не назвал."""
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get('https://api.bybit.com/v5/market/tickers',
                             params={'category': 'linear', 'symbol': symbol}) as r:
                data = json.loads(await r.text())
        return Decimal(data['result']['list'][0]['lastPrice'])
    except Exception:                                  # noqa: BLE001
        return None


def read_key():
    key = os.getenv('UPSCALE_API_KEY', '').strip()
    path = os.path.join(ROOT, '.probe_key')
    if not key and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as fh:
            key = fh.read().strip()
    return key


async def main(fill):
    key = read_key()
    if not key.startswith('usk_'):
        print('Нет ключа: положите его в Copier/.probe_key (одна строка usk_…)')
        return 2
    client = UpscaleClient(key)

    accounts = await call('accounts', client.accounts()) or []
    demo = next((a for a in accounts if a.get('type') == 'demo' and a.get('status') == 'active'
                 and a.get('apiTrading')), None)
    if demo is None:
        print('Нет активного ДЕМО-счёта с API Trading — дальше не иду (реальный счёт не трогаю).')
        return 3
    acc = demo['accountId']
    note('status_parsed', {k: str(v) for k, v in status_from_account(demo).items()})
    await call('risk_status', client.risk_status(acc))
    await call('stats', client.stats(acc))

    markets = await call('markets', client.markets(acc)) or []
    btc = mk.Markets(markets).find('BTCUSDT')
    if btc is None:
        print('BTC на счёте нет')
        return 4
    note('btc_market', btc)
    note('btc_parsed', {'max_leverage': str(mk.max_leverage(btc, 5)),
                        'price_step': str(mk.step(btc, 'price')), 'size_step': str(mk.step(btc, 'size'))})

    positions = await call('positions_before', client.active_positions(acc)) or []
    price = None
    for p in positions:
        price = from_fp9(pick(p, ('markPrice', 'indexPrice')))
    price = price or from_fp9(pick(btc, ('indexPrice', 'markPrice', 'price', 'lastPrice'))) \
        or from_fp9(pick(btc.get('config') or {}, ('indexPrice', 'markPrice')))
    if not price and os.getenv('PROBE_PRICE'):
        price = Decimal(os.getenv('PROBE_PRICE'))
    if not price:
        price = await bybit_price('BTCUSDT')
    if not price:
        price = Decimal(input('Текущая цена BTC (для заявки далеко от рынка): ').strip())
    note('price_used', str(price))

    # 3–4. Лимит на 30% ниже рынка (не нальётся) со стопом и тейком, потом правка и снятие.
    limit = mk.round_price(price * Decimal('0.7'), mk.step(btc, 'price'))
    size = Decimal('0.001')
    body = entry_order(acc, btc['id'], 'LONG', limit, size, Decimal(2), limit * size / 2,
                       stop=limit * Decimal('0.98'), take=limit * Decimal('1.05'))
    note('limit_body', body)
    order = await call('limit_with_sl_tp', client.create_order(body, new_idem_key()))
    await call('orders_after_limit', client.active_orders(acc))
    if order and order.get('id'):
        await call('patch_limit_price', client.update_trigger(order['id'], limit * Decimal('0.99'),
                                                              new_idem_key()))
        await call('orders_after_patch', client.active_orders(acc))
        await call('cancel_limit', client.cancel_order(order['id']))
    # Тот же лимит без amount-оценки — как сервер понимает amount при sizeMode=base.
    body0 = dict(body, amount='0')
    order0 = await call('limit_amount_zero', client.create_order(body0, new_idem_key()))
    if order0 and order0.get('id'):
        await call('cancel_limit_zero', client.cancel_order(order0['id']))
    # Стоп-заявка на вход (у мастера stop_entry): триггер на 30% выше рынка — не сработает.
    trig = mk.round_price(price * Decimal('1.3'), mk.step(btc, 'price'))
    sbody = entry_order(acc, btc['id'], 'LONG', trig, size, Decimal(2), trig * size / 2,
                        stop=trig * Decimal('0.98'), take=trig * Decimal('1.05'), kind='stop_market')
    note('stop_market_body', sbody)
    sorder = await call('stop_market_with_sl_tp', client.create_order(sbody, new_idem_key()))
    await call('orders_after_stop_market', client.active_orders(acc))
    if sorder and sorder.get('id'):
        await call('cancel_stop_market', client.cancel_order(sorder['id']))
    await call('orders_after_cleanup', client.active_orders(acc))

    if fill:
        # 5. Крошечная позиция по рынку со стопом 3% — смотрим всё вокруг неё.
        mbody = entry_order(acc, btc['id'], 'LONG', price, size, Decimal(2), price * size / 2,
                            stop=price * Decimal('0.97'), kind='market')
        await call('market_entry', client.create_order(mbody, new_idem_key()))
        time.sleep(2)
        positions = await call('positions_after_market', client.active_positions(acc)) or []
        pos = next((p for p in positions if str(pick(p, ('market', 'marketId'))) == str(btc['id'])), None)
        orders = await call('orders_after_market', client.active_orders(acc)) or []
        if pos:
            pid = str(pick(pos, ('idx', 'id', 'positionId')))
            half = (from_fp9(pick(pos, ('size', 'baseSize'))) or size) / 2
            for i, mult in enumerate((Decimal('1.05'), Decimal('1.08'))):
                await call(f'take_{i + 1}', client.create_order(
                    close_order(acc, btc['id'], 'LONG', pid, half, trigger=price * mult), new_idem_key()))
            orders = await call('orders_with_takes', client.active_orders(acc)) or []
            stop = next((o for o in orders if str(o.get('type')) in ('stop', 'stop_market')), None)
            if stop:
                # Два переноса подряд: PATCH отдаёт новый ордер — второй идёт по его id.
                moved = await call('patch_stop', client.update_trigger(stop['id'], price * Decimal('0.975'),
                                                                       new_idem_key())) or {}
                if moved.get('id'):
                    await call('patch_stop_again', client.update_trigger(moved['id'], price * Decimal('0.98'),
                                                                         new_idem_key()))
                await call('orders_after_stop_patch', client.active_orders(acc))
            await call('close_market', client.create_order(
                close_order(acc, btc['id'], 'LONG', pid, from_fp9(pick(pos, ('size', 'baseSize')))),
                new_idem_key()))
            time.sleep(2)
            await call('position_after_close', client.position(pid))
            await call('orders_after_close', client.active_orders(acc))
            await call('history', client.position_history(acc, limit=5))
        # Уборка: всё, что осталось по BTC от проверки (тейки/стопы без позиции), снять.
        for o in await client.active_orders(acc) or []:
            if str(o.get('market')) == str(btc['id']):
                await call(f'cleanup_{o.get("type")}', client.cancel_order(o['id']))
        await call('orders_final', client.active_orders(acc))
        await call('positions_final', client.active_positions(acc))

    out_dir = os.getenv('PROBE_OUT') or os.path.join(ROOT, 'probe_results')
    out = os.path.join(out_dir, datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(LOG, fh, ensure_ascii=False, indent=1, default=str)
    print(f'\nВсе ответы: {out}')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--fill', action='store_true', help='открыть и закрыть крошечную позицию (демо)')
    sys.exit(asyncio.run(main(ap.parse_args().fill)))
