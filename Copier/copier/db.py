"""
База копировщика (SQLite, WAL): участники, ключи (зашифрованы), счета Upscale,
сделки участников, отказы, эквити, журнал действий, настройки.

Писатель один — служба копировщика (все обращения под одной блокировкой).
Время — UTC ISO8601. Образец схемы — db.py удалённой платформы (e11c7cd^).
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from copier import config

# Путь участника (с 26.10.2026): /start — new (регистрируется: Upscale, ID, ключ);
# подключил счёт — pending (на проверке у владельца); active — бот торгует на его
# счёте; inactive — владелец отключил.
STATUSES = ('new', 'pending', 'active', 'inactive')
LEVELS = ('observer', 'demo', 'full')                # наблюдатель / только демо / демо и реальные
OPEN_STATES = ('new', 'pending', 'open')             # сделка ещё живёт
TRADE_STATES = OPEN_STATES + ('closed', 'canceled', 'error')

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id         INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    status        TEXT DEFAULT 'new',
    level         TEXT DEFAULT 'demo',
    max_accounts  INTEGER DEFAULT 1,
    paused        INTEGER DEFAULT 0,    -- пауза самого участника
    daily_summary INTEGER DEFAULT 1,
    upscale_uid   TEXT,                 -- ID на Upscale, который прислал участник
    uid_at        TEXT,
    created_at    TEXT,
    approved_at   TEXT,
    notes         TEXT
);
CREATE TABLE IF NOT EXISTS keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER,
    enc         TEXT,
    fingerprint TEXT,
    upscale_user TEXT,                  -- userId владельца ключа по ответу Upscale
    status      TEXT DEFAULT 'ok',      -- ok / dead
    last_error  TEXT,
    created_at  TEXT,
    checked_at  TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER,
    key_id      INTEGER,
    upscale_id  TEXT UNIQUE,
    kind        TEXT,                   -- demo / real / tournament
    nominal     REAL,
    phase       TEXT,
    enabled     INTEGER DEFAULT 1,
    paused      INTEGER DEFAULT 0,      -- пауза админа или участника по счёту
    status_json TEXT,
    status_at   TEXT,
    last_error  TEXT,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER,
    tg_id         INTEGER,
    master_key    TEXT,
    master_trade  INTEGER,
    strategy      TEXT,
    pair          TEXT,
    market_id     TEXT,
    direction     TEXT,
    state         TEXT,
    limit_price   TEXT,
    stop          TEXT,
    initial_stop  TEXT,
    targets       TEXT,
    fractions     TEXT,
    base_size     TEXT,
    filled_size   TEXT,
    leverage      TEXT,
    risk_usd      TEXT,
    entry_order   TEXT,
    entry_idem    TEXT,
    stop_order    TEXT,
    take_orders   TEXT,                 -- JSON [{id, idem, price, size}]
    position_id   TEXT,
    entry_price   TEXT,
    exit_price    TEXT,
    pnl_usd       TEXT,
    pnl_r         TEXT,
    exit_reason   TEXT,
    note          TEXT,
    created_at    TEXT,
    opened_at     TEXT,
    closed_at     TEXT,
    updated_at    TEXT,
    UNIQUE(account_id, master_key)
);
CREATE INDEX IF NOT EXISTS idx_trades_state   ON trades(state);
CREATE INDEX IF NOT EXISTS idx_trades_master  ON trades(master_key);
CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account_id);
CREATE TABLE IF NOT EXISTS refusals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT,
    account_id  INTEGER,
    tg_id       INTEGER,
    master_key  TEXT,
    strategy    TEXT,
    pair        TEXT,
    reason      TEXT
);
CREATE TABLE IF NOT EXISTS master_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT,
    kind        TEXT,
    master_key  TEXT,
    strategy    TEXT,
    pair        TEXT,
    payload     TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    account_id  INTEGER,
    at          TEXT,
    balance     REAL,
    equity      REAL,
    day_start   REAL
);
CREATE INDEX IF NOT EXISTS idx_equity ON equity(account_id, at);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT,
    actor       TEXT,
    action      TEXT,
    details     TEXT
);
CREATE TABLE IF NOT EXISTS tg_state (
    tg_id       INTEGER PRIMARY KEY,
    state       TEXT,
    data        TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
"""


def now_iso(dt=None):
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec='seconds')


def parse_iso(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _row(r):
    return dict(r) if r is not None else None


class DB:
    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        if self.path != ':memory:':
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            if self.path != ':memory:':
                self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self):
        """Базы до 26.10.2026: вход был по приглашению, статусы pending/active/blocked."""
        for table, column in (('users', 'upscale_uid'), ('users', 'uid_at'), ('keys', 'upscale_user')):
            cols = {r[1] for r in self.conn.execute(f'PRAGMA table_info({table})')}
            if column not in cols:
                self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')
        self.conn.execute("UPDATE users SET status='inactive' WHERE status='blocked'")
        self.conn.execute("UPDATE users SET status='new' WHERE status='pending' AND upscale_uid IS NULL")

    # ── низкий уровень ─────────────────────────────────────────────────────
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def q1(self, sql, args=()):
        with self.lock:
            return _row(self.conn.execute(sql, args).fetchone())

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    def _update(self, table, key_col, key, fields):
        if not fields:
            return
        cols = ', '.join(f'{k}=?' for k in fields)
        self.x(f'UPDATE {table} SET {cols} WHERE {key_col}=?', tuple(fields.values()) + (key,))

    # ── настройки ──────────────────────────────────────────────────────────
    def setting(self, key, default=None):
        r = self.q1('SELECT value FROM settings WHERE key=?', (key,))
        if r is None:
            return default
        try:
            return json.loads(r['value'])
        except ValueError:
            return default

    def set_setting(self, key, value):
        self.x('INSERT INTO settings(key, value) VALUES(?, ?) '
               'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, json.dumps(value, ensure_ascii=False)))

    def entries_paused(self):
        return bool(self.setting('entries_paused', False))

    def source_account(self):
        """Счёт бота, сделки которого копируются (админка, «Источник»)."""
        return str(self.setting('source_account', config.SOURCE_ACCOUNT) or config.SOURCE_ACCOUNT)

    # ── участники ──────────────────────────────────────────────────────────
    def user(self, tg_id):
        return self.q1('SELECT * FROM users WHERE tg_id=?', (int(tg_id),))

    def touch_user(self, tg_id, username=None, first_name=None):
        """Первое появление (/start) — регистрация со статусом new; дальше — обновить имя."""
        u = self.user(tg_id)
        if u is None:
            self.x('INSERT INTO users(tg_id, username, first_name, status, level, created_at) '
                   'VALUES(?, ?, ?, ?, ?, ?)',
                   (int(tg_id), username, first_name, 'new', 'demo', now_iso()))
        elif (username, first_name) != (u.get('username'), u.get('first_name')):
            self._update('users', 'tg_id', int(tg_id),
                         {'username': username, 'first_name': first_name})
        return self.user(tg_id)

    def set_user(self, tg_id, **fields):
        self._update('users', 'tg_id', int(tg_id), fields)

    def users(self, status=None):
        if status:
            return self.q('SELECT * FROM users WHERE status=? ORDER BY created_at', (status,))
        return self.q('SELECT * FROM users ORDER BY created_at')

    def activate(self, tg_id, level=None):
        """
        Владелец открыл доступ. Уровень — переданный; не передан — по подключённому
        счёту, который владелец только что проверил: реальный — полный доступ, демо —
        демо. Иначе галочка «активен» при уровне «демо» молча не дала бы торговать
        на реальном счёте.
        """
        if level not in LEVELS:
            kinds = {a.get('kind') for a in self.accounts(tg_id=tg_id, enabled_only=True)}
            level = 'full' if kinds - {'demo'} else 'demo'
        self.set_user(tg_id, status='active', level=level, approved_at=now_iso())

    def deactivate(self, tg_id):
        """Владелец отключил: новых сделок нет; открытые доводятся вслед за мастером."""
        self.set_user(tg_id, status='inactive')

    def key_users(self, tg_id):
        """userId владельцев ключей участника по ответу Upscale — сверить с присланным ID."""
        return sorted({k['upscale_user'] for k in self.q('SELECT upscale_user FROM keys WHERE tg_id=?',
                                                          (int(tg_id),)) if k.get('upscale_user')})

    # ── ключи ──────────────────────────────────────────────────────────────
    def add_key(self, tg_id, enc, fingerprint, upscale_user=None):
        return self.x('INSERT INTO keys(tg_id, enc, fingerprint, upscale_user, created_at, checked_at) '
                      'VALUES(?, ?, ?, ?, ?, ?)',
                      (int(tg_id), enc, fingerprint, upscale_user, now_iso(), now_iso()))

    def key(self, key_id):
        return self.q1('SELECT * FROM keys WHERE id=?', (key_id,))

    def set_key(self, key_id, **fields):
        self._update('keys', 'id', key_id, fields)

    def forget_keys(self, tg_id):
        """Отключение участника: ключи стираются физически, счета выключаются."""
        self.x('DELETE FROM keys WHERE tg_id=?', (int(tg_id),))
        self.x('UPDATE accounts SET enabled=0, key_id=NULL WHERE tg_id=?', (int(tg_id),))

    # ── счета ──────────────────────────────────────────────────────────────
    def add_account(self, tg_id, key_id, upscale_id, kind, nominal, phase):
        existing = self.q1('SELECT * FROM accounts WHERE upscale_id=?', (upscale_id,))
        if existing:
            if existing['tg_id'] != int(tg_id):
                return None
            self._update('accounts', 'id', existing['id'],
                         {'key_id': key_id, 'enabled': 1, 'kind': kind, 'nominal': nominal,
                          'phase': phase, 'last_error': None})
            return existing['id']
        return self.x('INSERT INTO accounts(tg_id, key_id, upscale_id, kind, nominal, phase, '
                      'created_at) VALUES(?, ?, ?, ?, ?, ?, ?)',
                      (int(tg_id), key_id, upscale_id, kind, nominal, phase, now_iso()))

    def account(self, account_id):
        return self.q1('SELECT * FROM accounts WHERE id=?', (account_id,))

    def accounts(self, tg_id=None, enabled_only=False):
        sql, args = 'SELECT * FROM accounts WHERE 1=1', []
        if tg_id is not None:
            sql += ' AND tg_id=?'
            args.append(int(tg_id))
        if enabled_only:
            sql += ' AND enabled=1'
        return self.q(sql + ' ORDER BY id', tuple(args))

    def set_account(self, account_id, **fields):
        if 'status_json' in fields and not isinstance(fields['status_json'], (str, type(None))):
            fields['status_json'] = json.dumps(fields['status_json'], ensure_ascii=False, default=str)
        self._update('accounts', 'id', account_id, fields)

    @staticmethod
    def account_status(acc):
        try:
            return json.loads(acc.get('status_json') or '{}')
        except ValueError:
            return {}

    # ── сделки ─────────────────────────────────────────────────────────────
    def create_trade(self, **fields):
        """Новая сделка участника; если по (счёт, мастер) уже есть — вернуть её id."""
        existing = self.q1('SELECT id FROM trades WHERE account_id=? AND master_key=?',
                           (fields['account_id'], fields['master_key']))
        if existing:
            return existing['id'], False
        for k in ('targets', 'fractions', 'take_orders'):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], default=str)
        fields = {k: (str(v) if hasattr(v, 'as_tuple') else v) for k, v in fields.items()}
        fields.setdefault('created_at', now_iso())
        fields['updated_at'] = now_iso()
        cols = ', '.join(fields)
        marks = ', '.join('?' for _ in fields)
        return self.x(f'INSERT INTO trades({cols}) VALUES({marks})', tuple(fields.values())), True

    def trade(self, trade_id):
        return self.q1('SELECT * FROM trades WHERE id=?', (trade_id,))

    def update_trade(self, trade_id, **fields):
        for k in ('targets', 'fractions', 'take_orders'):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], default=str)
        fields = {k: (str(v) if hasattr(v, 'as_tuple') else v) for k, v in fields.items()}
        fields['updated_at'] = now_iso()
        self._update('trades', 'id', trade_id, fields)

    def trades_for_master(self, master_key, states=OPEN_STATES):
        marks = ', '.join('?' for _ in states)
        return self.q(f'SELECT * FROM trades WHERE master_key=? AND state IN ({marks})',
                      (master_key,) + tuple(states))

    def open_trades(self, account_id=None, tg_id=None):
        marks = ', '.join('?' for _ in OPEN_STATES)
        sql, args = f'SELECT * FROM trades WHERE state IN ({marks})', list(OPEN_STATES)
        if account_id is not None:
            sql += ' AND account_id=?'
            args.append(account_id)
        if tg_id is not None:
            sql += ' AND tg_id=?'
            args.append(int(tg_id))
        return self.q(sql + ' ORDER BY id', tuple(args))

    def trades(self, account_id=None, tg_id=None, strategy=None, states=None, since=None,
               limit=500):
        sql, args = 'SELECT * FROM trades WHERE 1=1', []
        for col, val in (('account_id', account_id), ('tg_id', tg_id), ('strategy', strategy)):
            if val is not None:
                sql += f' AND {col}=?'
                args.append(val)
        if states:
            sql += f' AND state IN ({", ".join("?" for _ in states)})'
            args.extend(states)
        if since:
            sql += ' AND created_at>=?'
            args.append(since)
        sql += ' ORDER BY id DESC'
        if limit:
            sql += f' LIMIT {int(limit)}'
        return self.q(sql, tuple(args))

    # ── журналы ────────────────────────────────────────────────────────────
    def refuse(self, account_id, tg_id, master_key, strategy, pair, reason):
        self.x('INSERT INTO refusals(at, account_id, tg_id, master_key, strategy, pair, reason) '
               'VALUES(?, ?, ?, ?, ?, ?, ?)',
               (now_iso(), account_id, tg_id, master_key, strategy, pair, reason))

    def refusals(self, limit=300, account_id=None):
        if account_id is not None:
            return self.q('SELECT * FROM refusals WHERE account_id=? ORDER BY id DESC LIMIT ?',
                          (account_id, limit))
        return self.q('SELECT * FROM refusals ORDER BY id DESC LIMIT ?', (limit,))

    def master_event(self, ev):
        payload = {k: v for k, v in ev.items() if k not in ('kind', 'key')}
        self.x('INSERT INTO master_events(at, kind, master_key, strategy, pair, payload) '
               'VALUES(?, ?, ?, ?, ?, ?)',
               (now_iso(), ev.get('kind'), ev.get('key'), ev.get('strategy'), ev.get('pair'),
                json.dumps(payload, ensure_ascii=False, default=str)))

    def master_events(self, limit=300):
        return self.q('SELECT * FROM master_events ORDER BY id DESC LIMIT ?', (limit,))

    def add_equity(self, account_id, balance, equity, day_start):
        self.x('INSERT INTO equity(account_id, at, balance, equity, day_start) VALUES(?, ?, ?, ?, ?)',
               (account_id, now_iso(), balance, equity, day_start))

    def equity_series(self, account_id, since=None):
        if since:
            return self.q('SELECT at, balance, equity, day_start FROM equity '
                          'WHERE account_id=? AND at>=? ORDER BY at', (account_id, since))
        return self.q('SELECT at, balance, equity, day_start FROM equity WHERE account_id=? '
                      'ORDER BY at', (account_id,))

    def audit(self, actor, action, details=''):
        if not isinstance(details, str):
            details = json.dumps(details, ensure_ascii=False, default=str)
        self.x('INSERT INTO audit(at, actor, action, details) VALUES(?, ?, ?, ?)',
               (now_iso(), str(actor), action, details))

    def audit_log(self, limit=300, action=None):
        if action:
            return self.q('SELECT * FROM audit WHERE action=? ORDER BY id DESC LIMIT ?',
                          (action, limit))
        return self.q('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,))

    # ── состояние диалога в Telegram ───────────────────────────────────────
    def tg_state(self, tg_id):
        r = self.q1('SELECT state, data FROM tg_state WHERE tg_id=?', (int(tg_id),))
        if r is None:
            return None, {}
        try:
            return r['state'], json.loads(r['data'] or '{}')
        except ValueError:
            return r['state'], {}

    def set_tg_state(self, tg_id, state, data=None):
        self.x('INSERT INTO tg_state(tg_id, state, data, updated_at) VALUES(?, ?, ?, ?) '
               'ON CONFLICT(tg_id) DO UPDATE SET state=excluded.state, data=excluded.data, '
               'updated_at=excluded.updated_at',
               (int(tg_id), state, json.dumps(data or {}, ensure_ascii=False), now_iso()))
