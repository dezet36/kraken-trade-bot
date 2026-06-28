"""
Крипто-платежи платформы (продление подписки).

Провайдер-абстракция. По умолчанию:
  - 'nowpayments', если задан NOWPAYMENTS_API_KEY;
  - иначе 'mock' (для тестов/демо — счёт создаётся, но оплата не подтверждается сама).
Сменить явно: PAYMENT_PROVIDER = nowpayments | mock.

Опрос статуса (без публичного вебхука — проще для VPS): poll_invoices() периодически
проверяет pending-инвойсы; при оплате идемпотентно продлевает подписку и уведомляет юзера.

ENV:
  PAYMENT_PROVIDER     — nowpayments | mock
  NOWPAYMENTS_API_KEY  — ключ API NOWPayments
  PAYMENT_CURRENCY     — актив оплаты (деф. usdttrc20)

Жизненный цикл (в БД, таблица payments):
  create_invoice(tid) -> запись pending + ссылка на оплату
  poll_invoices()     -> status=paid => mark_payment_paid (идемпотентно) =>
                         extend_subscription(sub_days) => уведомление
                      -> status=expired/failed => пометка, чтобы не опрашивать вечно
"""

import os
import time
from datetime import timezone

import requests

import db
import telegram_notify as tg
from logger import log

# Статусы провайдеров, означающие успешную оплату
_PAID_STATES    = {'paid', 'finished', 'confirmed', 'completed'}
_FAILED_STATES  = {'expired', 'failed', 'refunded', 'cancelled', 'canceled'}

_provider = None


def _fmt_until(dt) -> str:
    try:
        return dt.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    except Exception:
        return str(dt)


# ── Провайдеры ───────────────────────────────────────────────────────────────
class MockProvider:
    """Демо-провайдер: создаёт счёт, статус сам не меняет (остаётся pending).
    В тестах статус подменяется. Позволяет гонять весь пайплайн без реального API."""
    name = 'mock'

    def create(self, amount_usd, order_id, telegram_id):
        inv = f"mock_{order_id}"
        return {'invoice_id': inv, 'pay_url': f"https://pay.mock/{inv}", 'processor_ref': inv}

    def status(self, payment):
        return payment.get('status', 'pending')


class NowPaymentsProvider:
    """NOWPayments REST. Требует NOWPAYMENTS_API_KEY. Опрос статуса по order_id/payment."""
    name = 'nowpayments'
    BASE = 'https://api.nowpayments.io/v1'

    def __init__(self):
        self.key = os.getenv('NOWPAYMENTS_API_KEY', '')
        self.pay_currency = os.getenv('PAYMENT_CURRENCY', 'usdttrc20')

    def _headers(self):
        return {'x-api-key': self.key, 'Content-Type': 'application/json'}

    def create(self, amount_usd, order_id, telegram_id):
        resp = requests.post(
            f"{self.BASE}/invoice",
            headers=self._headers(),
            json={
                'price_amount':      amount_usd,
                'price_currency':    'usd',
                'pay_currency':      self.pay_currency,
                'order_id':          order_id,
                'order_description': f"Подписка Fibonacci Bot (tg {telegram_id})",
            },
            timeout=20,
        )
        resp.raise_for_status()
        d = resp.json()
        return {
            'invoice_id':    str(d.get('id') or order_id),
            'pay_url':       d.get('invoice_url'),
            'processor_ref': str(d.get('id') or order_id),
        }

    def status(self, payment):
        """Возвращает строковый статус. Best-effort: при ошибке -> 'pending'."""
        ref = payment.get('processor_ref') or payment.get('invoice_id')
        try:
            resp = requests.get(
                f"{self.BASE}/payment",
                headers=self._headers(),
                params={'invoiceId': ref},
                timeout=20,
            )
            if not resp.ok:
                return 'pending'
            data = resp.json()
            items = data.get('data') if isinstance(data, dict) else data
            if not items:
                return 'pending'
            st = (items[0] or {}).get('payment_status', 'pending')
            return str(st).lower()
        except Exception as e:
            log(f"NOWPayments status error ({ref}): {e}")
            return 'pending'


def _provider_name() -> str:
    explicit = os.getenv('PAYMENT_PROVIDER', '').lower()
    if explicit:
        return explicit
    return 'nowpayments' if os.getenv('NOWPAYMENTS_API_KEY') else 'mock'


def get_provider():
    global _provider
    if _provider is None:
        name = _provider_name()
        _provider = NowPaymentsProvider() if name == 'nowpayments' else MockProvider()
        log(f"💳 Платёжный провайдер: {_provider.name}")
    return _provider


def reset_provider():
    """Сброс кеша провайдера (для тестов/смены конфигурации)."""
    global _provider
    _provider = None


# ── Публичный API ────────────────────────────────────────────────────────────
def create_invoice(telegram_id: int) -> dict:
    """Создаёт счёт на оплату подписки. Возвращает {invoice_id, pay_url, amount, currency}."""
    amount   = db.get_int_setting('sub_price_usd', 30)
    currency = os.getenv('PAYMENT_CURRENCY', 'usdttrc20')
    order_id = f"{telegram_id}-{int(time.time())}"

    inv = get_provider().create(amount, order_id, telegram_id)
    db.create_payment(inv['invoice_id'], telegram_id, amount, currency,
                      processor_ref=inv.get('processor_ref'))
    return {
        'invoice_id': inv['invoice_id'],
        'pay_url':    inv.get('pay_url'),
        'amount':     amount,
        'currency':   currency,
    }


def poll_invoices() -> int:
    """Опрашивает pending-инвойсы. При оплате продлевает подписку и уведомляет.
    Возвращает количество подтверждённых оплат за вызов."""
    provider = get_provider()
    pending  = db.list_pending_payments()
    if not pending:
        return 0

    confirmed = 0
    for p in pending:
        try:
            status = provider.status(p)
        except Exception as e:
            log(f"⚠️ Опрос платежа {p.get('invoice_id')}: {e}")
            continue

        if status in _PAID_STATES:
            # Идемпотентность: extend только при первом переходе pending->paid
            if db.mark_payment_paid(p['invoice_id']):
                days = db.get_int_setting('sub_days', 30)
                db.extend_subscription(p['telegram_id'], days)
                user  = db.get_user(p['telegram_id'])
                until = db.access_until(user)
                tg.subscription_extended(days, _fmt_until(until), telegram_id=p['telegram_id'])
                log(f"💰 Оплата подтверждена: tg {p['telegram_id']} +{days} дн.")
                confirmed += 1
        elif status in _FAILED_STATES:
            db.set_payment_status(p['invoice_id'], 'expired')
            log(f"❌ Платёж {p['invoice_id']} истёк/отменён ({status})")

    return confirmed
