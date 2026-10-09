"""
Торговые счета в Telegram-панели — реорганизация, этап 8.

Владелец ведёт проп руками и смотрит биржевые счета с телефона: в меню —
«Счета» (сколько ждёт исполнения), карточка счёта (деньги этапа, правила,
позиции, что ждёт руки) с «Готово» прямо на карточке и аварийная остановка
биржевого счёта через подтверждение.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from control import telegram_panel as panel  # noqa: E402
from accounts import books, live, manual, onexchange  # noqa: E402

from test_accounts_onexchange import Candles, Venue, a_setup  # noqa: E402,F401


def buttons(kb):
    return [b['callback_data'] for row in kb['inline_keyboard'] for b in row]


def prop():
    code, _ = live.save({'name': 'HashHedge', 'kind': 'manual', 'strategies': ['SMCS'], 'enabled': True})
    return code


def collected(code_rules=None):
    from accounts import trading
    return {'accounts': [{**live.public(c, r), 'book': trading.report(c, r)}
                         for c, r in sorted(live.load().items())]}


class Ctl:
    """Контроллер Telegram без бота: только разбор кнопок."""

    def __init__(self, monkeypatch):
        telegram_bot = __import__('importlib').import_module('control.telegram_bot')
        self.ctl = telegram_bot.BotController()
        self.ctl.trade_manager = type('Paper', (), {'snapshot': lambda self: {}})()
        monkeypatch.setattr(self.ctl, '_collect', lambda: collected())


class TestViews:
    def test_no_accounts_says_where_to_add(self):
        text, kb = panel.accounts_view({})
        assert 'страница «Счета»' in text and buttons(kb) == ['m']

    def test_menu_counts_what_waits(self):
        prop()
        manual.offer('SMCS', a_setup())
        text, kb = panel.main_view({**collected(), 'strategies': {}})
        assert 'Торговые счета' in text and 'ждут исполнения <b>1</b>' in text
        assert 'ac' in buttons(kb)

    def test_card_shows_money_and_done_buttons(self):
        code = prop()
        manual.offer('SMCS', a_setup())
        text, kb = panel.account_view(collected(), code)
        assert 'Капитал' in text and 'Поставить заявку · BTCUSDT LONG' in text
        assert f'aa:{code}:1' in buttons(kb)
        assert not any(b.startswith('ak:') for b in buttons(kb))      # у пропа нечего «закрыть на бирже»


class TestActions:
    def test_done_from_the_card(self, monkeypatch):
        code = prop()
        manual.offer('SMCS', a_setup())
        toast, text, _kb = Ctl(monkeypatch).ctl._render(f'aa:{code}:1')
        assert toast.startswith('✅') and 'Ждут исполнения' not in text

    def test_exchange_kill_switch_goes_through_confirmation(self, monkeypatch):
        v = Venue()
        monkeypatch.setattr(onexchange, '_venue', lambda code, rules: v)
        code, _ = live.save({'name': 'Bybit демо', 'kind': 'exchange', 'strategies': ['SMCS']})
        live.set_keys(code, 'k', 's')
        live.save({'id': code, 'enabled': True})
        onexchange.offer('SMCS', a_setup())
        ctl = Ctl(monkeypatch).ctl
        _t, text, kb = ctl._render(f'a:{code}')
        assert f'ak:{code}' in buttons(kb)
        _t, text, kb = ctl._render(f'ak:{code}')
        confirm = [b for b in buttons(kb) if b.startswith('y:k:')][0]
        toast, text, _kb = ctl._render(confirm)
        assert toast == 'Готово' and 'счёт остановлен' in text
        assert live.get(code)['enabled'] is False and not books.state()[code]['pending']
