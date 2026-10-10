"""
ИИ ведёт торговый счёт (accounts/ai_control.py) — счёт по инструкциям.

Проверяется:
- счёт с ИИ не ставит заявку сразу: сетап, прошедший решение счёта, ждёт
  ответа ИИ; «открыть» — заявка с причиной ИИ; «пропустить» и нет ответа в
  срок — сделки нет, сообщение владельцу без кнопки;
- одобренный вход по рынку — по цене сейчас; сетап устарел — не открывается;
- ограничители действий: стоп только к цене, безубыток после +1R, подтянутый
  стоп не ближе 0.25R к цене, половина — один раз;
- действия применяются к книге и становятся инструкциями; закрытие — сделка
  с причиной AI;
- очередь к модели: допуск и обзор — в своих окнах часа, ответ ложится в книгу;
- счёт без ИИ работает как раньше; промт — только с экспортом.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from accounts import ai_control, books, live, manual  # noqa: E402
from execution import core  # noqa: E402
from test_accounts_manual import Client, T0, BAR, a_setup, bar, prop  # noqa: E402

H = 3_600_000


@pytest.fixture(autouse=True)
def env(monkeypatch):
    strategy_profile = __import__('importlib').import_module('strategies.strategy_profile')
    values = {'limit_offset_pct': 0.0, 'cost_limit_pct': 0.0, 'expiry_hours': 24.0,
              'cooldown_hours': 12.0, 'max_hold_hours': 0.0, 'drops_at_target': True,
              'fills_through_market': False}
    for name, value in values.items():
        monkeypatch.setattr(strategy_profile, name, lambda strategy, v=value: v)
    monkeypatch.setattr(books, '_notify', None)
    monkeypatch.setattr(books, '_outbox', [])
    monkeypatch.setattr(books, '_funding', {})
    from infra import config
    monkeypatch.setattr(config, 'LLM_SERVER_URL', 'http://127.0.0.1:1', raising=False)
    monkeypatch.setattr(ai_control, 'start', lambda: None)
    monkeypatch.setattr(manual, '_approved_prices', lambda client, accounts: {})
    return values


@pytest.fixture
def sent(monkeypatch):
    out = []
    books.notify_with(lambda name, item: out.append((name, item)))
    yield out
    books.notify_with(None)


def state(code):
    return books.state()[code]


def answer(code, pair, **verdict):
    with books.lock:
        state(code)['ai_wait'][pair]['answer'] = verdict


def open_position(code, pair='BTCUSDT'):
    """Счёт с ИИ: одобренный лимит налился."""
    manual.offer('SMCS', a_setup(pair), now_ms=T0)
    answer(code, pair, take=True, conf=4, reason='ок')
    manual.update(Client(), now_ms=T0 + 1)
    manual.update(Client({pair: [bar(1, 99.5, 101.0, close=100.0)]}), now_ms=T0 + 3 * BAR)
    return state(code)['positions'][pair]


class TestGate:
    def test_ai_account_asks_instead_of_placing(self, sent):
        code = prop(ai_control=True)
        assert manual.offer('SMCS', a_setup(), now_ms=T0) == [(code, None)]
        st = state(code)
        assert not st['pending'] and 'BTCUSDT' in st['ai_wait']
        assert 'Setup: LONG BTC' in st['ai_wait']['BTCUSDT']['question']
        assert not sent, 'владельцу — ничего, пока ИИ не решил'
        assert manual.offer('SMCS', a_setup(), now_ms=T0 + 1)[0][1] == 'ждёт решения ИИ'

    def test_take_places_with_the_reason(self, sent):
        code = prop(ai_control=True)
        manual.offer('SMCS', a_setup(), now_ms=T0)
        answer(code, 'BTCUSDT', take=True, conf=4, reason='по тренду')
        manual.update(Client(), now_ms=T0 + 60_000)
        st = state(code)
        assert 'BTCUSDT' in st['pending'] and not st['ai_wait']
        item = sent[-1][1]
        assert item['kind'] == 'place' and any('ИИ: открыть (4/5) — по тренду' in x for x in item['lines'])
        assert st['counts']['offered'] == 1, 'повторное решение не считается вторым предложением'

    def test_skip_tells_the_owner_without_a_button(self, sent):
        code = prop(ai_control=True)
        manual.offer('SMCS', a_setup(), now_ms=T0)
        answer(code, 'BTCUSDT', take=False, conf=3, reason='против BTC')
        manual.update(Client(), now_ms=T0 + 60_000)
        st = state(code)
        assert not st['pending'] and not st['ai_wait']
        item = sent[-1][1]
        assert item['kind'] == 'ai' and item['action'] is False and 'против BTC' in item['lines'][0]
        assert st['counts']['refused'].get('ИИ: пропустить') == 1

    def test_no_answer_in_time_is_a_skip(self, sent):
        code = prop(ai_control=True)
        manual.offer('SMCS', a_setup(), now_ms=T0)
        manual.update(Client(), now_ms=T0 + 3 * H)
        st = state(code)
        assert not st['pending'] and not st['ai_wait']
        assert 'не успел' in sent[-1][1]['title']

    def test_approved_market_entry_uses_the_price_now(self, sent, env, monkeypatch):
        strategy_profile = __import__('importlib').import_module('strategies.strategy_profile')
        monkeypatch.setattr(strategy_profile, 'fills_through_market', lambda s: True)
        code = prop(ai_control=True)
        manual.offer('SMCS', dict(a_setup(entry=100.5), market_price=100.0), now_ms=T0)
        answer(code, 'BTCUSDT', take=True, conf=5, reason='ок')
        monkeypatch.setattr(manual, '_approved_prices', lambda client, accounts: {'BTCUSDT': 100.2})
        manual.update(Client(), now_ms=T0 + 60_000)
        pos = state(code)['positions']['BTCUSDT']
        assert pos['entry_price'] == pytest.approx(100.2 * (1 + books.cfg().PAPER_SLIPPAGE_PCT), rel=1e-3) \
            or pos['entry_price'] == pytest.approx(100.2, rel=1e-3)

    def test_stale_setup_is_not_opened(self, sent, monkeypatch):
        code = prop(ai_control=True)
        manual.offer('SMCS', a_setup(), now_ms=T0)
        answer(code, 'BTCUSDT', take=True, conf=5, reason='ок')
        monkeypatch.setattr(manual, '_approved_prices', lambda client, accounts: {'BTCUSDT': 97.0})
        manual.update(Client(), now_ms=T0 + 60_000)
        assert not state(code)['pending'] and 'устарел' in sent[-1][1]['title']

    def test_account_without_ai_places_at_once(self):
        code = prop()
        manual.offer('SMCS', a_setup(), now_ms=T0)
        assert 'BTCUSDT' in state(code)['pending'] and not state(code).get('ai_wait')


class TestGuards:
    def pos(self, **kw):
        p = {'direction': 'LONG', 'entry_price': 100.0, 'initial_stop': 98.0, 'stop_loss': 98.0}
        p.update(kw)
        return p

    def test_breakeven_only_after_1r(self):
        assert ai_control.validate(self.pos(), {'do': 'be'}, 101.0)[0] is None
        assert ai_control.validate(self.pos(), {'do': 'be'}, 102.1)[:2] == ('stop', 100.0)
        assert ai_control.validate(self.pos(stop_loss=100.5), {'do': 'be'}, 103.0)[0] is None

    def test_trail_only_tightens_and_not_into_the_noise(self):
        assert ai_control.validate(self.pos(), {'do': 'trail', 'stop': 97.0}, 103.0)[0] is None
        assert ai_control.validate(self.pos(), {'do': 'trail', 'stop': 102.8}, 103.0)[0] is None
        assert ai_control.validate(self.pos(), {'do': 'trail', 'stop': 101.5}, 103.0)[:2] == ('stop', 101.5)
        short = self.pos(direction='SHORT', initial_stop=102.0, stop_loss=102.0)
        assert ai_control.validate(short, {'do': 'trail', 'stop': 98.5}, 97.0)[:2] == ('stop', 98.5)
        assert ai_control.validate(self.pos(), {'do': 'trail'}, 103.0)[0] is None

    def test_half_once_close_hold_unknown(self):
        assert ai_control.validate(self.pos(), {'do': 'half'}, 99.0)[:2] == ('half', 0.5)
        assert ai_control.validate(self.pos(ai_half=True), {'do': 'half'}, 99.0)[0] is None
        assert ai_control.validate(self.pos(), {'do': 'close'}, 99.0)[0] == 'close'
        assert ai_control.validate(self.pos(), {'do': 'hold'}, 99.0)[0] == 'hold'
        assert ai_control.validate(self.pos(), {'do': 'reverse'}, 99.0)[0] is None

    def test_market_part_counts_fees_and_slippage(self):
        pos = {'direction': 'LONG', 'entry_price': 100.0, 'size': 10.0, 'realized_pnl': 0.0, 'fees_paid': 0.0}
        exit_price, portion = core.take_market_part(pos, 0.5, 110.0, 0.001, 0.00055)
        assert portion == 5.0 and pos['size'] == 5.0 and exit_price == pytest.approx(109.89)
        assert pos['realized_pnl'] == pytest.approx(5 * 9.89)
        assert pos['fees_paid'] == pytest.approx(5 * 109.89 * 0.00055)


class TestActions:
    def test_breakeven_half_and_close_become_instructions(self, sent):
        code = prop(ai_control=True)
        pos = open_position(code)
        with books.lock:
            pos['last_price'] = 103.0
            state(code)['ai_actions'] = [{'pair': 'BTCUSDT', 'do': 'be', 'reason': 'разворот', 'applied': False}]
        manual.update(Client(), now_ms=T0 + 4 * BAR)
        pos = state(code)['positions']['BTCUSDT']
        assert pos['stop_loss'] == pos['entry_price'] and pos['breakeven_set']
        assert 'безубыток' in sent[-1][1]['title'] and sent[-1][1]['action'] is True

        with books.lock:
            state(code)['ai_actions'] = [{'pair': 'BTCUSDT', 'do': 'half', 'reason': 'фикс', 'applied': False}]
        size = pos['size']
        manual.update(Client(), now_ms=T0 + 5 * BAR)
        pos = state(code)['positions']['BTCUSDT']
        assert pos['size'] == pytest.approx(size / 2) and pos['ai_half'] and pos['realized_pnl'] > 0

        with books.lock:
            state(code)['ai_actions'] = [{'pair': 'BTCUSDT', 'do': 'close', 'reason': 'всё', 'applied': False}]
        manual.update(Client(), now_ms=T0 + 6 * BAR)
        assert 'BTCUSDT' not in state(code)['positions']
        last = books.read_journal()[-1]
        assert last['exit_reason'] == 'AI' and last['pnl_usd'] > 0
        assert not state(code)['ai_actions']

    def test_refused_action_changes_nothing(self, sent):
        code = prop(ai_control=True)
        pos = open_position(code)
        stop = pos['stop_loss']
        with books.lock:
            pos['last_price'] = 100.5
            state(code)['ai_actions'] = [{'pair': 'BTCUSDT', 'do': 'be', 'reason': 'рано', 'applied': False}]
        n = len(sent)
        manual.update(Client(), now_ms=T0 + 4 * BAR)
        assert state(code)['positions']['BTCUSDT']['stop_loss'] == stop and len(sent) == n


class TestQueue:
    def test_gate_job_in_its_window_and_answer_lands_in_the_book(self):
        code = prop(ai_control=True)
        manual.offer('SMCS', a_setup(), now_ms=T0)
        at = T0 - T0 % H + 20 * 60_000
        job = ai_control.next_job(at)
        assert job == ('gate', code, 'BTCUSDT')
        ai_control.run_job(job, ask_fn=lambda s, q, n: ('{"take": "yes", "conf": 4, "reason": "ок"}', 50.0),
                           now_ms=at)
        assert state(code)['ai_wait']['BTCUSDT']['answer']['take'] is True
        assert ai_control.next_job(T0 - T0 % H + 2 * 60_000) is None, 'начало часа — тетради'

    def test_review_job_once_an_hour(self):
        code = prop(ai_control=True)
        open_position(code)
        at = T0 - T0 % H + 40 * 60_000
        job = ai_control.next_job(at)
        assert job == ('review', code, None)
        reply = '{"actions": [{"pair": "BTC", "do": "hold", "stop": null, "reason": "по плану"}]}'
        ai_control.run_job(job, ask_fn=lambda s, q, n: (reply, 80.0), now_ms=at)
        acts = state(code)['ai_actions']
        assert acts[0]['pair'] == 'BTCUSDT' and acts[0]['do'] == 'hold'
        assert ai_control.next_job(at + 60_000) is None, 'обзор — раз в час'

    def test_review_question_lists_positions(self):
        code = prop(ai_control=True)
        open_position(code)
        text, pairs = ai_control.review_question(state(code), live.get(code), T0 + H)
        assert pairs == ['BTCUSDT'] and 'BTC LONG (SMCS)' in text and 'R' in text

    def test_parse_review_keeps_known_pairs_only(self):
        out = ai_control.parse_review('{"actions": [{"pair": "ETH", "do": "close"}, {"pair": "BTC", "do": "be"}]}',
                                      ['BTCUSDT'])
        assert list(out) == ['BTCUSDT'] and out['BTCUSDT']['do'] == 'be'
        assert ai_control.parse_review('нет', ['BTCUSDT']) is None


class TestRules:
    def test_docs_export_matches_the_prompt(self):
        if not os.path.isdir(os.path.dirname(ai_control.EXPORT)):
            pytest.skip('нет docs/')
        assert open(ai_control.EXPORT, encoding='utf-8').read() == ai_control.export_text(), \
            'промт ИИ-контроля изменён без экспорта: python -m accounts.ai_control export'

    def test_flag_is_saved_with_the_account(self):
        code = prop(ai_control=True)
        assert live.get(code)['ai_control'] is True
        assert ai_control.enabled(live.get(code))
        assert not ai_control.enabled({**live.get(code), 'kind': 'exchange'}), 'пока только счёт по инструкциям'
