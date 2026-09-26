"""
Уведомления бумажного счёта (26.09.2026).

До переделки вход был пятью строками без размера и процента стопа, выход — без
цены входа, выхода и времени, о взятых целях и безубытке бумажный счёт не
сообщал вовсе; сводка дня считала сделки из памяти процесса и уходила заново
после каждой выкатки; запуск подписывал бумажный счёт «🔴 LIVE». Владелец
выключил сообщения о сделках 18.09 — читать их было незачем.
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_notify as tg  # noqa: E402

BAR_MS = 5 * 60 * 1000
T0 = 1_700_000_000_000


def position(**over):
    pos = {'strategy': 'LLM', 'pair': 'DOGEUSDT', 'direction': 'LONG', 'entry_price': 100.0,
           'planned_entry': 100.0, 'stop_loss': 97.0, 'initial_stop': 97.0,
           'targets': [103.0, 106.0], 'fractions': [0.5, 0.5], 'risk_amount': 100.0,
           'size': 33.3, 'initial_size': 33.3, 'cost_share_pct': 2.5,
           'opened_ts': T0 + 10 * 60_000, 'placed_ts': T0, 'breakeven_set': False,
           'be_level': 103.0, 'context': {'why': 'откат к уровню; цена < L8'}}
    pos.update(over)
    return pos


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, '_allowed', lambda event: True)
    monkeypatch.setattr(tg, '_strategy_on', lambda strategy: True)
    monkeypatch.setattr(tg, '_send', lambda text, chat_id=None, reply_markup=None:
                        sent.append((text, reply_markup)) or True)
    return sent


class TestTheEntryReadsLikeATrade:

    def test_numbers_risk_why_and_a_button(self, outbox):
        tg.paper_entry('LLM', 'DOGEUSDT', position(), balance=10_000)
        text, kb = outbox[-1]
        for piece in ('Вход · DOGEUSDT LONG', 'ИИ', 'Стоп 97.0000', '−3.00%', 'Цель 1 103.0000',
                      '+1.00R', 'Цель 2 106.0000', '+2.00R', 'Риск $100.00 (1.00% депозита)',
                      'Издержки ≈ 2.5% риска', 'лимит ждал 10м', 'цена &lt; L8'):
            assert piece in text, piece
        assert kb['inline_keyboard'][0][0]['callback_data'] == '!p:LLM:DOGEUSDT'

    def test_a_market_entry_says_how_it_filled(self, outbox):
        tg.paper_entry('LLM', 'DOGEUSDT', position(entry_price=99.5, planned_entry=100.0,
                                                   entry_note='по рынку: лимит 100 за ценой 99.5'))
        text, _kb = outbox[-1]
        assert 'по плану 100.0000' in text and 'по рынку' in text

    def test_the_event_switch_and_the_strategy_switch_silence_it(self, outbox, monkeypatch):
        monkeypatch.setattr(tg, '_allowed', lambda event: event != 'trade_opened')
        assert tg.paper_entry('LLM', 'DOGEUSDT', position()) is False
        monkeypatch.setattr(tg, '_allowed', lambda event: True)
        monkeypatch.setattr(tg, '_strategy_on', lambda strategy: strategy != 'LLM')
        assert tg.paper_entry('LLM', 'DOGEUSDT', position()) is False
        assert not outbox


class TestTargetsBreakevenAndExit:

    def test_a_partial_target_names_what_was_fixed_and_what_is_left(self, outbox):
        tg.paper_target('LLM', 'DOGEUSDT', position(size=16.65, breakeven_set=True), 0, 103.0)
        text, _kb = outbox[-1]
        for piece in ('Цель 1 из 2', 'закрыто 50%', '+$49.95', '+0.50R', 'Осталось 50%', 'стоп в безубытке'):
            assert piece in text, piece

    def test_breakeven(self, outbox):
        tg.paper_breakeven('LLM', 'DOGEUSDT', position(stop_loss=100.05, breakeven_set=True))
        assert 'Безубыток · DOGEUSDT LONG' in outbox[-1][0] and '100.0500' in outbox[-1][0]

    def test_the_exit_has_the_path_the_result_and_the_costs(self, outbox):
        row = {'strategy': 'LLM', 'pair': 'DOGEUSDT', 'direction': 'LONG', 'entry_price': 100.0,
               'exit_price': 106.0, 'exit_reason': 'TP2', 'targets_all': '103;106', 'duration_min': 200,
               'pnl_usd': 190.0, 'pnl_r': 1.9, 'fees_usd': 3.0, 'funding_usd': 0.5, 'mfe_r': 2.1,
               'mae_r': -0.4, 'balance_after': 10_190.0}
        tg.paper_exit(row)
        text, _kb = outbox[-1]
        for piece in ('Закрыта в плюс · DOGEUSDT LONG', 'цель 2 из 2', '100.0000 → 106.0000', '3ч 20м',
                      '+$190.00', '+1.90R', 'издержки $3.50', 'лучший +2.10R', 'худший −0.40R',
                      'депозит $10 190'):
            assert piece in text, piece

    @pytest.mark.parametrize('reason,word', [('SL', 'стоп'), ('BE', 'безубыток'),
                                             ('TIME', 'предел удержания'), ('MANUAL', 'вручную')])
    def test_every_exit_reason_is_named(self, outbox, reason, word):
        tg.paper_exit({'strategy': 'SMC', 'pair': 'XRPUSDT', 'direction': 'SHORT', 'entry_price': 2,
                       'exit_price': 2.1, 'exit_reason': reason, 'targets_all': '1.8', 'pnl_usd': -100,
                       'pnl_r': -1, 'balance_after': 9_900})
        assert word in outbox[-1][0] and 'Закрыта в минус' in outbox[-1][0]


@pytest.fixture()
def broker_env(monkeypatch):
    monkeypatch.setenv('TRADING_MODE', 'PAPER')
    monkeypatch.setenv('PAPER_FUNDING', 'false')
    for module in ('config', 'paper_broker', 'dashboard', 'shadow', 'setup_journal'):
        sys.modules.pop(module, None)
    import config
    import paper_broker
    for key, value in (('PAPER_FEE_MAKER', 0.0), ('PAPER_FEE_TAKER', 0.0), ('PAPER_SLIPPAGE_PCT', 0.0),
                       ('RISK_PER_TRADE', 1.0), ('LIMIT_ENTRY_OFFSET_PCT', 0.0)):
        monkeypatch.setattr(config, key, value)

    class Client:
        candles = {}

        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            rows = [c for c in self.candles.get(symbol, []) if since is None or c[0] >= since]
            return rows[:limit] if limit else rows

        def fetch_funding_rate(self, symbol):
            raise RuntimeError('нет')

    client = Client()
    return paper_broker.PaperBroker(client, strategies=('SMC',)), client, paper_broker


def smc_signal():
    return {'trading_pair': 'XRPUSDT', 'strategy': 'SMC',
            'setup': {'type': 'LONG', 'start_price': 80.0, 'end_price': 120.0, 'size': 40.0},
            'trigger': {'zone': 'OB'}, 'htf_trend': 'BULLISH',
            'params': {'entry': 100.0, 'stop_loss': 90.0, 'take_profit_1': 110.0, 'take_profit_2': 120.0,
                       'tp_targets': [110.0, 120.0], 'tp_fractions': [0.5, 0.5], 'be_level': None,
                       'breakeven_after_tp': True, 'max_same_direction': 0, 'rr': 1.0,
                       'position_size': 1.0, 'risk_amount': 100.0}}


class TestTheBrokerTellsAboutEveryStep:

    def test_fill_target_and_exit_are_announced(self, broker_env, monkeypatch):
        broker, client, pb = broker_env
        calls = []
        for name in ('paper_entry', 'paper_target', 'paper_breakeven', 'paper_exit'):
            monkeypatch.setattr(tg, name, (lambda n: lambda *a, **k: calls.append((n, a)))(name))
        pb._now_ms = lambda: T0
        broker.open('SMC', smc_signal())
        bars = [(101, 99.5, 100), (111, 100, 110), (121, 110, 120)]
        client.candles['XRPUSDT'] = [[T0 + i * BAR_MS, low, high, low, close, 0]
                                     for i, (high, low, close) in enumerate(bars)]
        pb._now_ms = lambda: T0 + 10 * BAR_MS
        broker.update()
        names = [n for n, _a in calls]
        assert names == ['paper_entry', 'paper_target', 'paper_exit'], names
        _strategy, _pair, pos, index, level = calls[1][1]
        assert (index, level) == (0, 110.0) and pos['breakeven_set'] is True
        row = calls[2][1][0]
        assert row['exit_reason'] == 'TP2' and row['pair'] == 'XRPUSDT'

    def test_a_telegram_failure_does_not_stop_the_trade(self, broker_env, monkeypatch):
        broker, client, pb = broker_env
        monkeypatch.setattr(tg, 'paper_entry', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('сеть')))
        pb._now_ms = lambda: T0
        broker.open('SMC', smc_signal())
        client.candles['XRPUSDT'] = [[T0, 100, 101, 99.5, 100, 0]]
        pb._now_ms = lambda: T0 + 3 * BAR_MS
        broker.update()
        assert broker.positions('SMC').get('XRPUSDT'), 'отказ уведомления сорвал вход'


class TestTheDailyReport:

    def test_once_a_day_and_from_the_journal(self, broker_env, outbox):
        broker, client, pb = broker_env
        pb._now_ms = lambda: T0
        broker.open('SMC', smc_signal())
        client.candles['XRPUSDT'] = [[T0, 100, 101, 99.5, 100, 0]]
        pb._now_ms = lambda: T0 + 3 * BAR_MS
        broker.update()
        broker.close_one('SMC', 'XRPUSDT')
        closed = datetime.fromtimestamp((T0 + 3 * BAR_MS) / 1000, timezone.utc)
        next_day = closed.replace(hour=0, minute=5) + (closed - closed.replace(hour=0, minute=0)).__class__(days=1)
        assert tg.daily_report_once(broker, now=next_day) is True
        text = outbox[-1][0]
        assert 'Итог ' + closed.strftime('%d.%m.%Y') in text and 'SMC · 1 сд' in text
        assert tg.daily_report_once(broker, now=next_day) is False, 'второй раз за сутки — нет'
        import telegram_state
        assert telegram_state.load()['daily_sent'] == closed.date().isoformat()

    def test_a_switched_off_report_is_still_marked_as_handled(self, broker_env, outbox, monkeypatch):
        broker, _client, _pb = broker_env
        monkeypatch.setattr(tg, '_allowed', lambda event: event != 'daily')
        now = datetime(2026, 9, 27, 0, 5, tzinfo=timezone.utc)
        assert tg.daily_report_once(broker, now=now) is False
        import telegram_state
        assert telegram_state.load()['daily_sent'] == '2026-09-26' and not outbox


class TestTheStartMessage:

    def test_paper_is_called_paper_and_the_switch_works(self, broker_env, outbox, monkeypatch):
        broker, _client, _pb = broker_env
        import config
        monkeypatch.setattr(config, 'TRADING_MODE', 'PAPER')
        tg.bot_started(broker.get_real_balance(), broker=broker)
        assert 'бумажный счёт' in outbox[-1][0] and 'LIVE' not in outbox[-1][0]
        monkeypatch.setattr(tg, '_allowed', lambda event: event != 'service')
        assert tg.bot_started(1.0, broker=broker) is False


class TestTheTokenNeverReachesTheLog:

    def test_errors_are_scrubbed(self, monkeypatch):
        # config модуля уведомлений: другие проверки перезагружают config, и
        # свежий import дал бы другой объект.
        monkeypatch.setattr(tg.config, 'TELEGRAM_BOT_TOKEN', '123456:SECRET-token')
        assert 'SECRET' not in tg._safe(Exception('url: /bot123456:SECRET-token/sendMessage'))
