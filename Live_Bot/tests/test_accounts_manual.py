"""
Счета «по инструкциям» (accounts/manual.py) — реорганизация, этап 7, шаг 2.

Проп без API: бот решает по правилам счёта и говорит, что сделать руками.
Проверяется:
  - сетап становится инструкцией с ценами, объёмом и риском от капитала счёта;
  - правила счёта: стороны, одна позиция на пару, пределы позиций, кулдаун
    стратегии; правила пропа — сделка не берётся, если худший исход открытых
    вместе с ней нарушит дневной убыток или просадку;
  - ведение по ядру исполнения: налив, стоп, тейки с безубытком, срок заявки,
    срок удержания — каждое событие, где нужна рука, становится инструкцией;
  - цель пропа и пробитый предел закрывают этап; новый этап — из правил;
  - действия владельца: «Готово», заявка снята, позиция закрыта, новый этап;
  - выключенный счёт ведёт открытое; удалить счёт с заявками нельзя, код
    удалённого не достаётся новому;
  - бот отдаёт копию сетапа без денег и не меняет решение теста;
  - Telegram и панель.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from accounts import books, live, manual  # noqa: E402

BAR = books.BAR_MS
T0 = 300_000 * 5_866_667          # начало пятиминутки, октябрь 2025
H = 3_600_000


class Client:
    """Биржа без сети: свечи по парам и нулевой фандинг."""

    def __init__(self, bars=None):
        self.bars = bars or {}

    def fetch_ohlcv(self, pair, timeframe, since=None, limit=500):
        return [b for b in self.bars.get(pair, []) if b[0] >= (since or 0)][:limit]

    def fetch_funding_rate(self, pair):
        return {'fundingRate': 0.0}


def bar(k, low, high, close=None, open_=None):
    """Свеча номер k после T0 (минимум, максимум)."""
    close = close if close is not None else (low + high) / 2
    return [T0 + k * BAR, open_ if open_ is not None else close, high, low, close, 1.0]


def a_setup(pair='BTCUSDT', side='LONG', entry=100.0, stop=98.0, targets=(106.0,), fractions=(1.0,),
            **params):
    p = {'entry': entry, 'stop_loss': stop, 'rr': 3.0, 'tp_targets': list(targets),
         'tp_fractions': list(fractions), 'breakeven_after_tp': False}
    p.update(params)
    return {'trading_pair': pair, 'setup': {'type': side}, 'params': p,
            'trigger': {'entry_type': 'LIMIT'}}


@pytest.fixture(autouse=True)
def profile(monkeypatch):
    """Величины исполнения стратегии — простые и известные."""
    import strategy_profile
    values = {'limit_offset_pct': 0.0, 'cost_limit_pct': 0.0, 'expiry_hours': 24.0,
              'cooldown_hours': 12.0, 'max_hold_hours': 0.0, 'drops_at_target': True,
              'fills_through_market': False}
    for name, value in values.items():
        monkeypatch.setattr(strategy_profile, name, lambda strategy, v=value: v)
    monkeypatch.setattr(books, '_notify', None)
    monkeypatch.setattr(books, '_outbox', [])
    monkeypatch.setattr(books, '_funding', {})
    return values


@pytest.fixture
def sent(monkeypatch):
    out = []
    books.notify_with(lambda name, item: out.append((name, item)))
    yield out
    books.notify_with(None)


def prop(**rules):
    base = {'name': 'Проп', 'kind': 'manual', 'strategies': ['SMCS'], 'enabled': True,
            'deposit': 10_000, 'risk_pct': 1, 'profit_target_pct': 0, 'max_drawdown_pct': 0,
            'daily_loss_pct': 0}
    base.update(rules)
    code, _ = live.save(base)
    return code


def book(code):
    return manual.report(code, live.get(code), now_ms=T0)


def kinds(sent):
    return [item['kind'] for _, item in sent]


# ── Сетап → заявка ──────────────────────────────────────────────────────────

class TestOffer:
    def test_setup_becomes_an_instruction_with_numbers(self, sent):
        code = prop()
        assert manual.offer('SMCS', a_setup(), now_ms=T0) == [(code, None)]
        order = book(code)['pending'][0]
        assert order['entry'] == 100.0 and order['stop'] == 98.0
        assert order['size'] == pytest.approx(50.0) and order['risk'] == 100.0   # 1% от 10 000 на 2 пункта
        name, item = sent[0]
        assert name == 'Проп' and item['kind'] == 'place' and item['action'] is True
        assert 'BTCUSDT LONG' in item['title']
        text = '\n'.join(item['lines'])
        for part in ('Лимит на вход: 100.0000', 'Стоп: 98.0000 (-2.00%)', 'Тейк: 106.0000 (+6.00%)',
                     'Объём: 50.0', 'Риск: $100.00 — 1.00% счёта'):
            assert part in text, part

    def test_several_takes_are_listed_with_shares(self, sent):
        prop()
        manual.offer('SMCS', a_setup(targets=(104.0, 108.0), fractions=(0.5, 0.5)), now_ms=T0)
        text = '\n'.join(sent[0][1]['lines'])
        assert 'Тейк 1: 104.0000 (+4.00%) — закрыть 50%' in text
        assert 'Тейк 2: 108.0000 (+8.00%) — закрыть 50%' in text

    def test_only_accounts_that_chose_the_strategy(self, sent):
        prop(strategies=['SMC'])
        assert manual.offer('SMCS', a_setup(), now_ms=T0) == []
        assert not manual.wants('SMCS') and manual.wants('SMC')

    def test_disabled_account_takes_nothing(self):
        prop(enabled=False)
        assert manual.offer('SMCS', a_setup(), now_ms=T0) == []
        assert not manual.wants('SMCS')

    def test_account_rules(self):
        code = prop(sides='short', max_slots=2, max_same_direction=1)
        assert manual.offer('SMCS', a_setup(), now_ms=T0)[0][1].startswith('сторона')
        short = dict(side='SHORT', entry=100.0, stop=102.0, targets=(94.0,))
        assert manual.offer('SMCS', a_setup(**short), now_ms=T0) == [(code, None)]
        assert manual.offer('SMCS', a_setup(**short), now_ms=T0)[0][1] == 'пара занята'
        assert manual.offer('SMCS', a_setup(pair='ETHUSDT', **short), now_ms=T0)[0][1].startswith('в одну сторону')
        counts = book(code)['counts']
        assert counts['offered'] == 4 and counts['orders'] == 1
        assert counts['refused'] == {'сторона': 1, 'пара занята': 1, 'в одну сторону': 1}

    def test_slots(self):
        prop(max_slots=1)
        manual.offer('SMCS', a_setup(), now_ms=T0)
        assert manual.offer('SMCS', a_setup(pair='ETHUSDT'), now_ms=T0)[0][1].startswith('позиций')

    def test_cooldown_of_the_strategy(self, profile):
        code = prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        manual.act(code, 'cancel', pair='BTCUSDT', now_ms=T0 + H)
        assert manual.offer('SMCS', a_setup(), now_ms=T0 + 2 * H)[0][1] == 'кулдаун'
        assert manual.offer('SMCS', a_setup(), now_ms=T0 + 13 * H) == [(code, None)]

    def test_daily_rule_leaves_room_for_the_worst_case(self):
        """Дневной предел $150: одна сделка с риском $100 помещается, вторая — нет."""
        prop(daily_loss_pct=1.5)
        assert manual.offer('SMCS', a_setup(), now_ms=T0)[0][1] is None
        why = manual.offer('SMCS', a_setup(pair='ETHUSDT'), now_ms=T0)[0][1]
        assert why.startswith('правила пропа') and 'дневной убыток 1.5%' in why

    def test_drawdown_rule_leaves_room_for_the_worst_case(self):
        prop(max_drawdown_pct=0.9)          # пол $9 910: риск $100 уже не помещается
        why = manual.offer('SMCS', a_setup(), now_ms=T0)[0][1]
        assert why.startswith('правила пропа') and 'макс. просадка 0.9%' in why

    def test_cost_limit_of_the_strategy(self, monkeypatch):
        import strategy_profile
        monkeypatch.setattr(strategy_profile, 'cost_limit_pct', lambda s: 1.0)
        prop()
        why = manual.offer('SMCS', a_setup(stop=99.9), now_ms=T0)[0][1]
        assert why.startswith('предел издержек')

    def test_limit_already_through_the_market_is_entered_at_market(self, sent, monkeypatch):
        import strategy_profile
        monkeypatch.setattr(strategy_profile, 'fills_through_market', lambda s: True)
        code = prop()
        setup = a_setup()
        setup['market_price'] = 99.0
        manual.offer('SMCS', setup, now_ms=T0)
        assert kinds(sent) == ['market']
        b = book(code)
        assert not b['pending'] and b['positions'][0]['entry'] == pytest.approx(99.0 * 1.0005)

    def test_setup_copy_carries_no_money(self):
        from strategies import contract
        signal = a_setup(risk_pct=1.0, risk_amount=100.0, position_size=5.0, max_same_direction=3)
        signal['scan'] = {'score': 1}
        copy = books.setup_copy(signal)
        assert not set(copy['params']) & set(contract.MONEY_KEYS)
        assert signal['params']['risk_amount'] == 100.0              # оригинал не тронут
        assert copy['params']['entry'] == 100.0 and copy['setup']['type'] == 'LONG'


# ── Ведение ─────────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_fill_then_stop(self, sent):
        code = prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        client = Client({'BTCUSDT': [bar(1, 99.8, 100.4), bar(2, 97.5, 99.0)]})
        manual.update(client, now_ms=T0 + 4 * BAR)
        assert kinds(sent) == ['place', 'fill', 'exit']
        assert sent[1][1]['action'] is True                       # проверить стоп после налива
        assert sent[2][1]['action'] is False and 'стоп' in sent[2][1]['title']
        b = book(code)
        assert not b['positions'] and b['balance'] < 9_900       # −1R и издержки
        row = books.read_journal()[-1]
        assert row['account'] == code and row['exit_reason'] == 'SL' and row['outcome'] == 'минус'
        assert row['pnl_r'] == pytest.approx(-1.0, abs=0.08)
        assert b['quality']['trades'] == 1 and b['quality']['losses'] == 1

    def test_partial_take_asks_to_move_the_stop(self, sent):
        code = prop()
        manual.offer('SMCS', a_setup(targets=(104.0, 108.0), fractions=(0.5, 0.5),
                                     breakeven_after_tp=True), now_ms=T0)
        client = Client({'BTCUSDT': [bar(1, 99.8, 100.4), bar(2, 100.5, 104.5), bar(3, 99.5, 101.0)]})
        manual.update(client, now_ms=T0 + 5 * BAR)
        assert kinds(sent) == ['place', 'fill', 'target', 'exit']
        target = sent[2][1]
        assert target['action'] is True
        assert 'Перенесите стоп остатка в безубыток: 100.0000' in '\n'.join(target['lines'])
        row = books.read_journal()[-1]
        assert row['exit_reason'] == 'BE' and row['pnl_usd'] > 0 and row['tps_hit'] == 1
        assert book(code)['balance'] > 10_000

    def test_order_that_did_not_fill_in_time_is_cancelled(self, sent, monkeypatch):
        import strategy_profile
        monkeypatch.setattr(strategy_profile, 'expiry_hours', lambda s: 1.0)
        code = prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        client = Client({'BTCUSDT': [bar(k, 101.0, 102.0) for k in range(1, 15)]})
        manual.update(client, now_ms=T0 + 16 * BAR)
        assert kinds(sent) == ['place', 'cancel']
        assert 'срок заявки вышел' in sent[1][1]['lines'][0]
        assert book(code)['counts']['dropped'] == {'срок заявки вышел': 1}

    def test_hold_limit_is_a_close_instruction(self, sent, monkeypatch):
        import strategy_profile
        monkeypatch.setattr(strategy_profile, 'max_hold_hours', lambda s: 1.0)
        prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        client = Client({'BTCUSDT': [bar(1, 99.8, 100.4)] + [bar(k, 100.5, 101.5) for k in range(2, 16)]})
        manual.update(client, now_ms=T0 + 17 * BAR)
        assert kinds(sent) == ['place', 'fill', 'close']
        assert sent[2][1]['action'] is True and 'Срок удержания 1 ч вышел' in sent[2][1]['lines'][0]
        assert books.read_journal()[-1]['exit_reason'] == 'TIME'

    def test_disabled_account_still_manages_what_is_open(self, sent):
        code = prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        live.save({'id': code, 'enabled': False})
        client = Client({'BTCUSDT': [bar(1, 99.8, 100.4), bar(2, 97.5, 99.0)]})
        manual.update(client, now_ms=T0 + 4 * BAR)
        assert kinds(sent)[-1] == 'exit'

    def test_notifier_failure_does_not_stop_the_account(self):
        def broken(name, item):
            raise RuntimeError('Telegram лежит')
        books.notify_with(broken)
        try:
            code = prop()
            assert manual.offer('SMCS', a_setup(), now_ms=T0) == [(code, None)]
        finally:
            books.notify_with(None)


# ── Правила пропа ───────────────────────────────────────────────────────────

class TestPropRules:
    def _open(self, code):
        manual.offer('SMCS', a_setup(), now_ms=T0)
        manual.offer('SMCS', a_setup(pair='ETHUSDT', entry=50.0, stop=49.0, targets=(53.0,)), now_ms=T0)
        manual.update(Client({'BTCUSDT': [bar(1, 99.8, 100.4)]}), now_ms=T0 + 3 * BAR)
        return book(code)

    def test_drawdown_breach_closes_everything(self, sent):
        code = prop()
        b = self._open(code)
        assert len(b['positions']) == 1 and len(b['pending']) == 1
        live.save({'id': code, 'max_drawdown_pct': 0.5})           # пол $9 950
        manual.update(Client({'BTCUSDT': [bar(2, 98.6, 99.2, close=98.7)]}), now_ms=T0 + 4 * BAR)
        b = book(code)
        assert b['status'] == books.FAILED and not b['positions'] and not b['pending']
        rules = sent[-1][1]
        assert rules['kind'] == 'rules' and rules['action'] is True
        text = '\n'.join(rules['lines'])
        assert 'Закрыть по рынку: BTCUSDT LONG' in text and 'Снять заявку: ETHUSDT LONG' in text
        assert books.read_journal()[-1]['exit_reason'] == 'RULES'
        assert not manual.wants('SMCS')
        assert manual.offer('SMCS', a_setup(pair='SOLUSDT'), now_ms=T0 + 5 * BAR) == []

    def test_daily_breach_closes_everything(self):
        code = prop()
        self._open(code)
        live.save({'id': code, 'daily_loss_pct': 0.5})
        manual.update(Client({'BTCUSDT': [bar(2, 98.6, 99.2, close=98.7)]}), now_ms=T0 + 4 * BAR)
        b = book(code)
        assert b['status'] == books.FAILED and 'за сутки' in b['status_why']

    def test_target_locks_the_phase(self, sent):
        code = prop()
        self._open(code)
        live.save({'id': code, 'profit_target_pct': 1.0})          # цель $10 100
        manual.update(Client({'BTCUSDT': [bar(2, 101.0, 102.6, close=102.5)]}), now_ms=T0 + 4 * BAR)
        b = book(code)
        assert b['status'] == books.TARGET and not b['positions']
        assert sent[-1][1]['kind'] == 'pass' and b['balance'] > 10_100

    def test_new_phase_takes_the_size_from_the_rules(self, sent):
        code = prop()
        self._open(code)
        with pytest.raises(ValueError):
            manual.act(code, 'new_phase', now_ms=T0 + 4 * BAR)        # позиции открыты
        live.save({'id': code, 'profit_target_pct': 1.0})
        manual.update(Client({'BTCUSDT': [bar(2, 101.0, 102.6, close=102.5)]}), now_ms=T0 + 4 * BAR)
        live.save({'id': code, 'deposit': 20_000})                  # ап-скейл
        manual.act(code, 'new_phase', now_ms=T0 + 5 * BAR)
        b = book(code)
        assert b['phase'] == 2 and b['status'] == books.ACTIVE
        assert b['start'] == b['balance'] == 20_000.0
        assert b['quality']['trades'] == 0 and b['quality_all']['trades'] == 1
        assert manual.wants('SMCS') and sent[-1][1]['kind'] == 'phase'

    def test_room_and_progress_on_the_panel(self):
        code = prop(profit_target_pct=10, max_drawdown_pct=10, daily_loss_pct=5)
        manual.offer('SMCS', a_setup(), now_ms=T0)
        b = book(code)
        assert b['target_level'] == 11_000 and b['floor'] == 9_000 and b['daily_limit'] == 500
        assert b['open_risk'] == 100.0
        assert b['room']['rule'] == 'дневной убыток 5%' and b['room']['amount'] == pytest.approx(400.0)


# ── Владелец ────────────────────────────────────────────────────────────────

class TestOwner:
    def test_done_mark(self, sent):
        code = prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        item = book(code)['waiting'][0]
        assert manual.act(code, 'ack', item=item['id'], now_ms=T0) == 'отмечено'
        assert book(code)['waiting'] == []
        with pytest.raises(ValueError):
            manual.act(code, 'ack', item=999)

    def test_owner_closed_and_cancelled(self):
        code = prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        manual.offer('SMCS', a_setup(pair='ETHUSDT'), now_ms=T0)
        manual.update(Client({'BTCUSDT': [bar(1, 99.8, 101.0, close=101.0)]}), now_ms=T0 + 3 * BAR)
        assert 'закрыта в записи' in manual.act(code, 'close', pair='BTCUSDT', now_ms=T0 + 3 * BAR)
        assert books.read_journal()[-1]['exit_reason'] == 'MANUAL'
        manual.act(code, 'cancel', pair='ETHUSDT')
        b = book(code)
        assert not b['positions'] and not b['pending']
        assert b['counts']['dropped'] == {'снята владельцем': 1}
        with pytest.raises(ValueError):
            manual.act(code, 'close', pair='BTCUSDT')
        with pytest.raises(ValueError):
            manual.act(code, 'взлететь')

    def test_book_follows_the_rules_size_until_the_first_order(self):
        code = prop()
        live.save({'id': code, 'deposit': 25_000})
        assert book(code)['start'] == 25_000
        manual.offer('SMCS', a_setup(), now_ms=T0)
        assert book(code)['pending'][0]['risk'] == 250.0
        live.save({'id': code, 'deposit': 30_000})
        b = book(code)
        assert b['start'] == 25_000 and b['deposit_rules'] == 30_000

    def test_busy_account_is_not_removed_and_its_code_is_not_reused(self):
        code = prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        manual.update(Client({'BTCUSDT': [bar(1, 99.8, 100.4), bar(2, 97.5, 99.0)]}), now_ms=T0 + 4 * BAR)
        manual.offer('SMCS', a_setup(pair='ETHUSDT'), now_ms=T0 + 5 * BAR)
        with pytest.raises(ValueError):
            live.remove(code)
        manual.act(code, 'cancel', pair='ETHUSDT')
        live.remove(code)
        assert code not in books.state()
        again = prop()
        assert again != code, 'сделки удалённого счёта попали бы в статистику нового'


# ── Бот и уведомления ───────────────────────────────────────────────────────

class TestWiring:
    def test_bot_hands_a_copy_and_the_test_decides_as_before(self):
        """Копия сетапа для торговых счетов не меняет сигнал теста."""
        import bot
        candidate = {'pair': 'BTCUSDT', 'signal': {
            'trading_pair': 'BTCUSDT', 'setup': {'type': 'LONG'},
            'params': {'entry': 1.0, 'stop_loss': 0.9, 'take_profit_1': 1.2, 'rr': 2.0},
            'levels': {'level': 1.0, 'touches': 2}}, 'score': 1.0, 'rr': 2.0, 'df_1h': None}
        plain, _ = bot._build_signal(json.loads(json.dumps(candidate)), 'LEVELS', 10_000)
        setups = []
        traced, _ = bot._build_signal(json.loads(json.dumps(candidate)), 'LEVELS', 10_000, setups=setups)
        assert plain == traced
        assert len(setups) == 1 and 'risk_pct' not in setups[0]['params']

    def _cycle(self, monkeypatch):
        """Один фантомный цикл бота без сети: три кандидата SMCS, у теста —
        одно свободное место. Возвращает заявки теста и ответы счетам."""
        import bot
        from data import liquidations
        import market_regime
        from data import trades_ws
        from strategies import registry

        pairs = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']

        def candidate(pair):
            return {'pair': pair, 'score': 1.0, 'rr': 3.0, 'df_1h': None,
                    'signal': {**a_setup(pair=pair), 'smcs': {'kind': 'BOS', 'stop_pct': 2.0}}}

        class Adapter:
            @staticmethod
            def scan(liquid, gate, client=None):
                return [candidate(p) for p in pairs]

            @staticmethod
            def build_signal(c):
                return c['signal'], None

        opened = []

        class Broker:
            strategies = ('SMCS',)
            client = Client()
            update = staticmethod(lambda: None)
            balance = equity = start_balance = staticmethod(lambda s: 10_000.0)
            slots_used_by = staticmethod(lambda s: 0)
            gate = staticmethod(lambda s: object())

            @staticmethod
            def open(strategy, signal):
                opened.append((strategy, signal['trading_pair'], signal['params']['risk_pct']))
                return True

        offers = []
        real_offer = manual.offer
        monkeypatch.setattr(manual, 'offer', lambda s, setup, now_ms=None:
                            offers.append(setup['trading_pair']) or real_offer(s, setup, now_ms=now_ms))
        monkeypatch.setattr(bot, 'broker', Broker(), raising=False)
        monkeypatch.setattr(bot.controller, 'is_paused', lambda: False)
        monkeypatch.setattr(bot, 'get_liquid_pairs', lambda client: list(pairs))
        monkeypatch.setattr(bot, '_watch_streams', lambda: None)
        monkeypatch.setattr(bot, '_poll_news', lambda: None)
        for module, name in ((liquidations, 'ensure_running'), (trades_ws, 'ensure_running'),
                             (market_regime, 'btc_regime'), (bot.positioning, 'collect_if_due'),
                             (bot.market_cap, 'collect_if_due'), (bot.market_mood, 'collect_if_due')):
            monkeypatch.setattr(module, name, lambda *a, **k: None)
        monkeypatch.setattr(registry, 'adapter', lambda code: Adapter)
        monkeypatch.setattr(bot.account, 'slots_free', lambda strategy, used: 1)
        bot._paper_cycle()
        return opened, offers

    def test_cycle_offers_every_candidate_and_the_test_is_unchanged(self, monkeypatch):
        import bot
        monkeypatch.setattr(bot.account, 'enabled', lambda s: s == 'SMCS')
        alone, offers = self._cycle(monkeypatch)
        assert alone == [('SMCS', 'BTCUSDT', bot.account.risk_pct('SMCS'))]
        assert offers == []                                             # счетов нет — как раньше

        code = prop()
        with_account, offers = self._cycle(monkeypatch)
        assert with_account == alone, 'торговый счёт изменил решение теста'
        assert offers == ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']              # и сверх предела теста
        assert len(book(code)['pending']) == 3

    def test_telegram_message(self, monkeypatch):
        tg = __import__('importlib').import_module('control.telegram_notify')
        out = []
        monkeypatch.setattr(tg, '_send', lambda text, **kw: out.append(text) or True)
        item = {'icon': '📥', 'title': 'Поставить заявку · BTCUSDT LONG', 'lines': ['Стоп: 98 <проверить>'],
                'action': True}
        assert tg.account_instruction('Проп', item)
        assert 'Поставить заявку · BTCUSDT LONG' in out[0] and '&lt;проверить&gt;' in out[0]
        assert '«Готово»' in out[0]

    def test_done_button_in_telegram(self, monkeypatch):
        """Кнопка «Готово» под инструкцией-действием отмечает её в книге счёта."""
        telegram_bot = __import__('importlib').import_module('control.telegram_bot')
        tg = __import__('importlib').import_module('control.telegram_notify')
        markups = []
        monkeypatch.setattr(tg, '_send', lambda text, reply_markup=None, **kw: markups.append(reply_markup) or True)
        books.notify_with(tg.account_instruction)
        try:
            code = prop()
            manual.offer('SMCS', a_setup(), now_ms=T0)
        finally:
            books.notify_with(None)
        data = markups[0]['inline_keyboard'][0][0]['callback_data']
        assert data == f'ad:{code}:1'
        ctl = telegram_bot.BotController()
        ctl.trade_manager = type('Paper', (), {'snapshot': lambda self: {}})()
        toast, text, _kb = ctl._render(data)
        assert toast.startswith('✅') and text is None
        assert book(code)['waiting'] == []
        toast, _t, _k = ctl._render(f'ad:{code}:999')
        assert 'не найдена' in toast

    def test_telegram_message_can_be_switched_off(self, monkeypatch):
        settings_store = __import__('importlib').import_module('accounts.settings_store')
        tg = __import__('importlib').import_module('control.telegram_notify')
        out = []
        monkeypatch.setattr(tg, '_send', lambda text, **kw: out.append(text) or True)
        settings_store.save({'NOTIFY': {'account_orders_telegram': False}})
        assert not tg.account_instruction('Проп', {'title': 'x', 'lines': []})
        assert out == []
        assert 'account_orders' in settings_store.NOTIFY_EVENTS


@pytest.fixture(scope='module')
def server():
    dashboard = __import__('importlib').import_module('control.dashboard')
    port = 8938
    threading.Thread(
        target=lambda: dashboard.start_dashboard(port=port, host='127.0.0.1'),
        daemon=True).start()
    time.sleep(2.0)
    return f'http://127.0.0.1:{port}'


class TestPanel:
    @pytest.fixture(autouse=True)
    def _control(self, monkeypatch):
        monkeypatch.setenv('DASHBOARD_ALLOW_CONTROL', 'true')

    @staticmethod
    def _call(url, body=None):
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode('utf-8') or '{}')

    def test_book_and_done_mark(self, server):
        code = prop()
        manual.offer('SMCS', a_setup())
        status, data = self._call(f'{server}/api/accounts')
        assert status == 200 and data['engine'] == {'manual': True, 'exchange': True}
        b = data['books'][code]
        assert len(b['pending']) == 1 and len(b['waiting']) == 1
        item = b['waiting'][0]['id']
        status, _ = self._call(f'{server}/api/accounts/act', {'id': code, 'action': 'ack', 'item': item})
        assert status == 200
        _, data = self._call(f'{server}/api/accounts')
        assert data['books'][code]['waiting'] == []
        status, data = self._call(f'{server}/api/accounts/act', {'id': code, 'action': 'new_phase'})
        assert status == 400 and 'закройте' in data['error']
