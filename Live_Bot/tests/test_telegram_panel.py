"""
Telegram-панель (26.09.2026): одно меню, которое перерисовывается на месте;
карточки позиций и заявок с управлением через подтверждение; статистика по
стратегиям; переключатели стратегий и уведомлений — общие с сайтом.

Проверки идут по тому, что владелец видел сломанным: «Позиции» не отвечали
(время входа с поясом минус время без пояса), бумажный счёт назывался
«🔴 LIVE», «Выгрузка» отвечала «журнал пуст», каждая кнопка слала новое
сообщение, пауза снималась первой же выкаткой, закрытие шло без вопроса.
"""

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BAR_MS = 5 * 60 * 1000
T0 = 1_700_000_000_000


class FakeClient:
    def __init__(self):
        self.candles = {}

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        rows = list(self.candles.get(symbol, []))
        if since is not None:
            rows = [c for c in rows if c[0] >= since]
        return rows[:limit] if limit else rows

    def fetch_funding_rate(self, symbol):
        raise RuntimeError('ставка недоступна')


def signal(pair='DOGEUSDT', direction='LONG', entry=100.0, stop=90.0, tp1=130.0,
           strategy='LLM', why='вход от уровня'):
    return {
        'trading_pair': pair, 'strategy': strategy,
        'setup': {'type': direction, 'start_price': 80.0, 'end_price': 120.0, 'size': 40.0},
        'trigger': {'zone': 'Zone_A'}, 'htf_trend': 'BULLISH',
        'params': {'entry': entry, 'stop_loss': stop, 'take_profit_1': tp1, 'take_profit_2': tp1,
                   'tp_targets': [tp1], 'tp_fractions': [1.0], 'be_level': None,
                   'breakeven_after_tp': False, 'max_same_direction': 0,
                   'rr': abs(tp1 - entry) / abs(entry - stop), 'position_size': 1.0,
                   'risk_amount': 100.0},
        'scan': {'score': 70.0}, 'llm': {'why': why},
    }


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv('TRADING_MODE', 'PAPER')
    monkeypatch.setenv('PAPER_FUNDING', 'false')
    for name in ('FIBO', 'SMC', 'LEVELS', 'RSIBB', 'LLM'):
        monkeypatch.setenv(f'PAPER_START_BALANCE_{name}', '10000')
    for module in ('config', 'paper_broker', 'dashboard', 'shadow', 'setup_journal', 'telegram_bot'):
        sys.modules.pop(module, None)
    import config
    import paper_broker
    import telegram_bot
    for key, value in (('PAPER_FEE_MAKER', 0.0), ('PAPER_FEE_TAKER', 0.0), ('PAPER_SLIPPAGE_PCT', 0.0),
                       ('RISK_PER_TRADE', 1.0), ('LIMIT_ENTRY_OFFSET_PCT', 0.0),
                       ('TELEGRAM_CHAT_ID', '42'), ('TELEGRAM_BOT_TOKEN', '')):
        monkeypatch.setattr(config, key, value)
    client = FakeClient()
    broker = paper_broker.PaperBroker(client, strategies=('FIBO', 'SMC', 'LLM'))
    ctl = telegram_bot.BotController()
    ctl.trade_manager = broker
    out = SimpleNamespace(broker=broker, client=client, ctl=ctl, pb=paper_broker, cfg=config,
                          tb=telegram_bot, sent=[], edited=[], answered=[])
    monkeypatch.setattr(ctl, '_send', lambda chat_id, text, reply_markup=None:
                        out.sent.append((text, reply_markup)) or {'ok': True})
    monkeypatch.setattr(ctl, '_edit', lambda chat_id, message_id, text, reply_markup=None:
                        out.edited.append((message_id, text, reply_markup)) or {'ok': True})
    monkeypatch.setattr(ctl, '_answer_callback', lambda cid, text='': out.answered.append(text))
    return out


def feed(env, pair, bars, start=T0):
    """bars: (high, low, close) — свечи по 5 минут, все закрыты."""
    env.client.candles[pair] = [[start + i * BAR_MS, low, high, low, close, 0]
                                for i, (high, low, close) in enumerate(bars)]
    env.pb._now_ms = lambda: start + (len(bars) + 1) * BAR_MS
    env.broker.update()


def open_position(env, strategy='LLM', pair='DOGEUSDT', **over):
    env.pb._now_ms = lambda: T0
    assert env.broker.open(strategy, signal(pair=pair, strategy=strategy, **over))
    entry = over.get('entry', 100.0)
    feed(env, pair, [(entry + 1, entry - 0.5, entry)])
    assert env.broker.positions(strategy).get(pair), 'позиция не открылась'


def codes(keyboard):
    return [b['callback_data'] for row in keyboard['inline_keyboard'] for b in row]


def code_starting(keyboard, prefix):
    return next(c for c in codes(keyboard) if c.startswith(prefix))


class TestWhatWasBroken:

    def test_the_paper_account_is_not_called_live(self, env):
        _toast, text, _kb = env.ctl._render('m')
        assert 'бумажный счёт' in text and 'LIVE' not in text

    def test_positions_open_with_a_timezone_aware_entry(self, env):
        """До 26.09.2026: TypeError — «can't subtract offset-naive and offset-aware»."""
        open_position(env)
        _t, text, kb = env.ctl._render('pl:0')
        assert 'DOGE' in text and 'p:LLM:DOGEUSDT' in codes(kb)
        _t, card, _kb = env.ctl._render('p:LLM:DOGEUSDT')
        for piece in ('DOGEUSDT · LONG', 'Вход', 'Стоп', 'Цель 1', 'риск'):
            assert piece in card, piece

    def test_a_button_edits_the_message_it_was_pressed_on(self, env):
        env.ctl._handle_callback('pl:0', '42', 777, 'cb')
        assert env.edited and env.edited[0][0] == 777 and not env.sent

    def test_a_button_under_a_notification_opens_a_new_message(self, env):
        open_position(env)
        env.ctl._handle_callback('!p:LLM:DOGEUSDT', '42', 777, 'cb')
        assert env.sent and not env.edited, 'уведомление не затирается картой позиции'

    def test_export_sends_the_paper_journal(self, env, monkeypatch):
        open_position(env)
        env.broker.close_one('LLM', 'DOGEUSDT')
        files = []
        monkeypatch.setattr(env.ctl, '_send_document', lambda chat_id, path: files.append(path) or True)
        env.ctl._send_export('42')
        assert sorted(os.path.basename(p) for p in files) == ['paper_trades.csv', 'paper_trades.jsonl']
        assert 'Выгружено файлов: 2' in env.sent[-1][0]

    def test_pause_survives_a_restart(self, env):
        env.ctl.set_paused(True)
        assert env.tb.BotController().is_paused() is True
        env.ctl.set_paused(False)
        assert env.tb.BotController().is_paused() is False

    def test_mute_survives_a_restart(self, env):
        env.ctl._handle_command('/mute', '42', [])
        assert env.tb.BotController().is_muted() is True


class TestNothingHappensWithoutAConfirmation:

    def test_closing_asks_first_then_closes(self, env):
        open_position(env)
        _t, text, kb = env.ctl._render('pc:LLM:DOGEUSDT')
        assert 'Закрыть DOGEUSDT' in text and env.broker.positions('LLM').get('DOGEUSDT')
        toast, done, _kb = env.ctl._render(code_starting(kb, 'y:c:'))
        assert toast == 'Готово' and not env.broker.positions('LLM').get('DOGEUSDT')
        assert 'закрыта' in done
        assert env.pb.read_journal()[-1]['exit_reason'] == 'MANUAL'

    def test_an_old_confirmation_does_nothing(self, env):
        """Нажатие, пролежавшее в очереди Telegram, пока бот перезапускался, — не действие."""
        open_position(env)
        stale = env.tb._stamp(time.time() - env.tb.CONFIRM_TTL_S - 60)
        toast, text, _kb = env.ctl._render(f'y:c:{stale}:LLM:DOGEUSDT')
        assert 'устарело' in toast and env.broker.positions('LLM').get('DOGEUSDT')

    def test_breakeven_moves_the_stop_to_entry_and_the_button_disappears(self, env):
        open_position(env)
        _t, _text, kb = env.ctl._render('pb:LLM:DOGEUSDT')
        env.ctl._render(code_starting(kb, 'y:b:'))
        pos = env.broker.positions('LLM')['DOGEUSDT']
        assert pos['stop_loss'] == pos['entry_price']
        _t, _card, kb = env.ctl._render('p:LLM:DOGEUSDT')
        assert not any(c.startswith('pb:') for c in codes(kb)), 'безубыток уже стоит'

    def test_an_order_is_cancelled_after_confirmation(self, env):
        env.pb._now_ms = lambda: T0
        env.broker.open('SMC', signal(pair='LTCUSDT', strategy='SMC'))
        _t, text, kb = env.ctl._render('o:SMC:LTCUSDT')
        assert 'заявка' in text
        _t, _text, kb = env.ctl._render('oc:SMC:LTCUSDT')
        env.ctl._render(code_starting(kb, 'y:x:'))
        assert not env.broker.pending('SMC')

    def test_close_command_leads_to_the_confirmation(self, env):
        open_position(env)
        env.ctl._handle_command('/close', '42', ['doge'])
        assert 'Закрыть DOGEUSDT' in env.sent[-1][0]


class TestSwitchesAreSharedWithTheSite:

    def test_turning_a_strategy_off_asks_and_writes_the_site_switch(self, env):
        import settings_store
        _t, text, kb = env.ctl._render('se:FIBO')
        assert 'Выключить входы' in text and settings_store.enabled('FIBO')
        env.ctl._render(code_starting(kb, 'y:f:'))
        assert settings_store.enabled('FIBO') is False
        toast, _text, _kb = env.ctl._render('se:FIBO')
        assert toast == 'Входы включены' and settings_store.enabled('FIBO') is True

    def test_notification_switches_write_the_shared_settings(self, env):
        import settings_store
        env.ctl._render('ne:trade_opened')
        assert settings_store.notify_on('trade_opened', 'telegram') is False
        env.ctl._render('ns:FIBO')
        assert settings_store.notify_strategy('FIBO') is False
        env.ctl._render('sn:FIBO')
        assert settings_store.notify_strategy('FIBO') is True

    def test_an_unknown_event_is_not_written(self, env):
        import settings_store
        env.ctl._render('ne:whatever')
        assert 'whatever_telegram' not in (settings_store.load().get('NOTIFY') or {})


class TestTheScreens:

    def test_statistics_are_split_by_strategy(self, env):
        open_position(env, strategy='LLM', pair='DOGEUSDT')
        open_position(env, strategy='SMC', pair='XRPUSDT')
        env.broker.close_one('LLM', 'DOGEUSDT')
        env.broker.close_one('SMC', 'XRPUSDT')
        _t, text, _kb = env.ctl._render('st:all')
        assert 'ИИ · 1 сд' in text and 'SMC · 1 сд' in text and 'Фибо · сделок нет' in text

    def test_the_model_text_is_escaped(self, env):
        open_position(env, why='цена < L8 & ждём')
        _t, card, _kb = env.ctl._render('p:LLM:DOGEUSDT')
        assert 'цена &lt; L8 &amp; ждём' in card

    def test_every_button_code_fits_the_telegram_limit(self, env):
        open_position(env, strategy='SMC', pair='1000PEPEUSDT')
        env.pb._now_ms = lambda: T0
        env.broker.open('LLM', signal(pair='SHIB1000USDT', strategy='LLM'))
        seen = []
        for code in ('m', 'pl:0', 'p:SMC:1000PEPEUSDT', 'pc:SMC:1000PEPEUSDT', 'pb:SMC:1000PEPEUSDT',
                     'ol:0', 'o:LLM:SHIB1000USDT', 'oc:LLM:SHIB1000USDT', 'ai', 'st:7', 'sl', 's:SMC',
                     'sa:SMC', 'nt', 'h'):
            _t, _text, kb = env.ctl._render(code)
            seen += codes(kb)
        assert seen and all(len(c.encode()) <= 64 for c in seen), [c for c in seen if len(c.encode()) > 64]

    def test_a_missing_part_of_a_code_falls_back_to_the_menu(self, env):
        _t, text, _kb = env.ctl._render('p')
        assert 'Капитал' in text

    def test_a_closed_position_card_says_so(self, env):
        _t, text, _kb = env.ctl._render('p:LLM:DOGEUSDT')
        assert 'позиции уже нет' in text

    def test_many_positions_are_paged(self, env):
        for k in range(12):
            open_position(env, strategy='FIBO', pair=f'C{k:02d}USDT')
        _t, text, kb = env.ctl._render('pl:0')
        assert 'pl:1' in codes(kb) and '1/2' in json.dumps(kb, ensure_ascii=False)
        _t, text2, _kb = env.ctl._render('pl:1')
        assert 'C11' in text2 and 'C11' not in text


class TestOnlyTheOwnerIsServed:

    def test_a_stranger_gets_nothing(self, env):
        env.ctl._handle_update({'update_id': 1, 'message': {'chat': {'id': 99}, 'text': '/menu'}})
        env.ctl._handle_update({'update_id': 2, 'callback_query': {
            'id': 'x', 'data': 'y:c:0:LLM:DOGEUSDT', 'message': {'chat': {'id': 99}, 'message_id': 5}}})
        assert not env.sent and not env.edited and not env.answered

    def test_the_owner_gets_the_menu(self, env):
        env.ctl._handle_update({'update_id': 1, 'message': {'chat': {'id': 42}, 'text': '/menu'}})
        assert env.sent and 'Капитал' in env.sent[-1][0]


class TestTheTokenNeverReachesTheLog:

    def test_errors_are_scrubbed(self, env, monkeypatch):
        monkeypatch.setattr(env.cfg, 'TELEGRAM_BOT_TOKEN', '123456:SECRET-token')
        text = env.ctl._safe(Exception('Max retries: /bot123456:SECRET-token/getUpdates'))
        assert 'SECRET' not in text and '<TOKEN>' in text
