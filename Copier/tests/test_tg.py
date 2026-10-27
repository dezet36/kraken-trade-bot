"""
Telegram участника на фальшивых Telegram и Upscale. Путь (с 26.10.2026):
/start → Upscale и «Готово» → ID → ключ → счёт → проверка → активация владельцем.
"""

import os

from copier import config, feed
from copier.db import DB
from copier.follower import Follower
from copier.tg import bot as tgbot
from copier.tg import screens
from copier.tg.api import TgApi
from copier.upscale import UpscaleClient
from tests.helpers import FakeBot, FakeUpscale, Notes, run

ADMIN = 111
USER = 42


class FakeTg:
    def __init__(self):
        self.sent, self.deleted = [], []

    async def send(self, method, payload):
        if method == 'deleteMessage':
            self.deleted.append(payload['message_id'])
        elif method in ('sendMessage', 'editMessageText'):
            self.sent.append((payload['chat_id'], payload['text'], payload.get('reply_markup')))
        return {'message_id': len(self.sent)}

    def last(self, chat_id):
        return next(s for s in reversed(self.sent) if s[0] == chat_id)

    def texts(self, chat_id):
        return [s[1] for s in self.sent if s[0] == chat_id]

    def buttons(self, chat_id):
        kb = self.last(chat_id)[2] or {'inline_keyboard': []}
        return [b.get('callback_data') or b.get('url') for row in kb['inline_keyboard'] for b in row]


def make(tmp_path):
    db = DB(':memory:')
    fake_up = FakeUpscale()
    tg = FakeTg()
    f = feed.Feed(account='upscale', state_path=str(tmp_path / 'source_state.json'),
                  fetch=FakeBot().fetch)
    factory = lambda key: UpscaleClient(key, send=fake_up.send)
    fol = Follower(db, f, Notes(), client_factory=factory)
    b = tgbot.TgBot(db, fol, f, api=TgApi(token='t', send=tg.send), client_factory=factory)
    return db, b, tg, fake_up


def msg(tg_id, text, mid=1):
    return {'message': {'message_id': mid, 'chat': {'id': tg_id, 'type': 'private'},
                        'from': {'id': tg_id, 'username': f'u{tg_id}'}, 'text': text}}


def press(tg_id, data):
    return {'callback_query': {'id': 'cb', 'data': data, 'from': {'id': tg_id},
                               'message': {'message_id': 5, 'chat': {'id': tg_id, 'type': 'private'}}}}


def test_onboarding_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'SIGNUP_URL', 'https://app.upscale.trade?ref=TEST')
    db, b, tg, fake_up = make(tmp_path)

    # 1. /start — сразу зарегистрирован; приветствие, Upscale, ссылка и «Готово».
    run(b.handle_update(msg(USER, '/start')))
    assert db.user(USER)['status'] == 'new'
    hello = tg.last(USER)[1]
    assert 'Upscale' in hello and 'зарегистрируйтесь' in hello
    assert 'https://app.upscale.trade?ref=TEST' in tg.buttons(USER) and 'rd' in tg.buttons(USER)
    assert 'реф' not in hello.lower() and 'правил' not in hello.lower()

    # Ключ раньше ID — удалён и не сохранён.
    run(b.handle_update(msg(USER, 'usk_too_early', mid=70)))
    assert 70 in tg.deleted and tg.last(USER)[1] == screens.ID_FIRST
    assert db.q1('SELECT * FROM keys WHERE tg_id=?', (USER,)) is None

    # 2. «Готово» → спрашиваем ID; не похоже на ID — просим снова.
    run(b.handle_update(press(USER, 'rd')))
    assert tg.last(USER)[1] == screens.ASK_ID
    run(b.handle_update(msg(USER, 'мой айди 123')))
    assert tg.last(USER)[1] == screens.BAD_ID
    run(b.handle_update(msg(USER, 'PL-12345')))
    assert db.user(USER)['upscale_uid'] == 'PL-12345'
    assert 'usk_' in tg.last(USER)[1]                     # дальше — API-ключ
    assert not [t for t in tg.texts(ADMIN)]                # владельца пока не дёргаем

    # 3. Ключ → единственный счёт подключается сам → на проверку.
    run(b.handle_update(msg(USER, 'usk_participant_key', mid=77)))
    assert 77 in tg.deleted
    stored = db.q1('SELECT * FROM keys WHERE tg_id=?', (USER,))
    assert stored and 'usk_participant_key' not in stored['enc'] and stored['upscale_user'] == 'user-1'
    assert db.user(USER)['status'] == 'pending'
    assert tg.last(USER)[1] == screens.SUBMITTED
    accounts = db.accounts(tg_id=USER, enabled_only=True)
    assert len(accounts) == 1 and accounts[0]['upscale_id'] == 'acc-1'
    card = tg.last(ADMIN)[1]
    assert 'PL-12345' in card and 'user-1' in card and 'сверьте' in card and 'демо' in card
    assert f'ua:{USER}:auto' in tg.buttons(ADMIN)

    # До активации меню закрыто — «на проверке».
    run(b.handle_update(press(USER, 'pl')))
    assert tg.last(USER)[1] == screens.UNDER_REVIEW

    # 4. Владелец активирует → уровень по счёту (демо), участнику «бот торгует».
    run(b.handle_update(press(ADMIN, f'ua:{USER}:auto')))
    assert db.user(USER)['status'] == 'active' and db.user(USER)['level'] == 'demo'
    assert tg.last(USER)[1] == screens.ACTIVATED and 'm' in tg.buttons(USER)
    run(b.handle_update(press(USER, 'm')))
    assert 'Копирование сделок' in tg.last(USER)[1]


def test_real_account_activates_full(tmp_path):
    db, b, tg, fake_up = make(tmp_path)
    fake_up.accounts_list[0]['type'] = 'real'
    run(b.handle_update(msg(USER, '/start')))
    run(b.handle_update(press(USER, 'rd')))
    run(b.handle_update(msg(USER, 'user-1')))
    run(b.handle_update(msg(USER, 'usk_k', mid=5)))
    assert '✓ совпадает' in tg.last(ADMIN)[1]
    db.activate(USER)
    assert db.user(USER)['level'] == 'full'


def test_resend_id_before_key(tmp_path):
    db, b, tg, _ = make(tmp_path)
    run(b.handle_update(msg(USER, '/start')))
    run(b.handle_update(press(USER, 'rd')))
    run(b.handle_update(msg(USER, 'abc123')))
    run(b.handle_update(msg(USER, '/start')))           # вернулся — его ведут к ключу, ID помнится
    assert 'abc123' in tg.last(USER)[1] and 'usk_' in tg.last(USER)[1]


def test_deactivated_user(tmp_path):
    db, b, tg, _ = make(tmp_path)
    db.touch_user(USER)
    db.activate(USER)
    run(b.handle_update(press(ADMIN, f'ub:{USER}')))
    assert db.user(USER)['status'] == 'inactive'
    assert tg.last(USER)[1] == screens.DEACTIVATED
    run(b.handle_update(msg(USER, '/menu')))
    assert tg.last(USER)[1] == screens.DEACTIVATED


def test_declined_on_review(tmp_path):
    db, b, tg, _ = make(tmp_path)
    db.touch_user(USER)
    db.set_user(USER, status='pending', upscale_uid='abc123')
    run(b.handle_update(press(ADMIN, f'ub:{USER}')))
    assert tg.last(USER)[1] == screens.DECLINED


def test_admin_is_active_at_once(tmp_path):
    db, b, tg, _ = make(tmp_path)
    run(b.handle_update(msg(ADMIN, '/start')))
    assert db.user(ADMIN)['status'] == 'active' and db.user(ADMIN)['level'] == 'full'


def test_stale_confirmation_refused(tmp_path):
    db, b, tg, _ = make(tmp_path)
    db.touch_user(USER)
    db.set_user(USER, status='active')
    old = tgbot._stamp(1)
    run(b.handle_update(press(USER, f'y:dx:0:{old}')))
    assert 'устарело' in tg.last(USER)[1]


def test_forged_close_of_foreign_trade_ignored(tmp_path):
    """callback_data присылает клиент: «y:tc:<чужая сделка>» без кнопки ничего не закрывает."""
    db, b, tg, _ = make(tmp_path)
    for who in (USER, 77):
        db.touch_user(who)
        db.set_user(who, status='active')
    trade_id, _ = db.create_trade(account_id=1, tg_id=77, master_key='BTCUSDT@1', strategy='', pair='BTCUSDT',
                                  market_id='m', direction='LONG', state='open', limit_price=100, stop=95,
                                  initial_stop=95, targets=[110], fractions=[1], base_size=1, leverage=5,
                                  risk_usd=5, entry_idem='i')
    closed = []

    async def close_trade(tid, actor, reason=None):
        closed.append(tid)
        return True, ''
    b.follower.close_trade = close_trade
    run(b.handle_update(press(USER, f'y:tc:{trade_id}:{tgbot._stamp()}')))
    assert closed == []
    run(b.handle_update(press(77, f'y:tc:{trade_id}:{tgbot._stamp()}')))
    assert closed == [trade_id]


def test_settings_show_source_risk(tmp_path):
    db, b, tg, _ = make(tmp_path)
    b.feed.st['snapshot'] = {'risk_pct': 1.0, 'pending': {}, 'positions': {}}
    db.touch_user(USER)
    db.set_user(USER, status='active')
    run(b.handle_update(press(USER, 'se')))
    assert 'Риск на сделку: 1% от баланса — как у счёта бота' in tg.last(USER)[1]
    assert not any(str(x).startswith('rk:') for x in tg.buttons(USER))


def test_old_database_migrates(tmp_path):
    """База до 26.10.2026: статусы pending/blocked, нет столбцов ID — переносится при открытии."""
    import sqlite3
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, status TEXT,
            level TEXT, risk_pct REAL, max_risk_pct REAL, max_accounts INTEGER, strategies TEXT,
            allowed TEXT, paused INTEGER, daily_summary INTEGER, invite_code TEXT, terms_at TEXT,
            created_at TEXT, approved_at TEXT, notes TEXT);
        CREATE TABLE keys (id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, enc TEXT,
            fingerprint TEXT, status TEXT, last_error TEXT, created_at TEXT, checked_at TEXT);
        INSERT INTO users(tg_id, status) VALUES (1, 'pending'), (2, 'blocked'), (3, 'active');
    """)
    conn.commit()
    conn.close()
    db = DB(path)
    assert [db.user(i)['status'] for i in (1, 2, 3)] == ['new', 'inactive', 'active']
    db.add_key(3, 'x', 'f', upscale_user='u')          # новый столбец есть
    assert db.key_users(3) == ['u']


def test_account_view_shows_risk_in_dollars():
    from copier import stats
    text, _ = screens.account_view({'id': 1, 'kind': 'real', 'enabled': 1},
                                   {'accountBalance': 25_000, 'currentEquity': 25_100},
                                   stats.summarize([]), 1.0)
    assert 'Риск на сделку: 1% от баланса = 250.00 $' in text
