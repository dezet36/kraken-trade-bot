"""
SQLite-слой платформы: пользователи, подписки/триал, платежи, журнал сделок, настройки.

Потокобезопасность: на каждый вызов открывается своя короткоживущая connection
(SQLite сам делает файловую блокировку), WAL включён для конкурентного чтения.
Время хранится в UTC ISO8601 (TEXT).
"""

import os
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv('PLATFORM_DB', os.path.join(os.path.dirname(__file__), 'platform.db'))

_write_lock = threading.Lock()   # сериализуем записи (SQLite single-writer)

DEFAULT_SETTINGS = {
    'trial_days':            '7',
    'sub_price_usd':         '30',
    'sub_days':              '30',
    'expiry_reminder_days':  '3',
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Схема ────────────────────────────────────────────────────────────────────
def init_db():
    with _write_lock, _connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id     INTEGER PRIMARY KEY,
            username        TEXT,
            joined_at       TEXT,
            status          TEXT DEFAULT 'active',   -- active/suspended/banned
            exchange        TEXT,                    -- bybit/bingx
            api_key_enc     TEXT,
            api_secret_enc  TEXT,
            paused          INTEGER DEFAULT 0,
            trial_until     TEXT,
            paid_until      TEXT,
            last_reminded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            invoice_id      TEXT PRIMARY KEY,
            telegram_id     INTEGER,
            amount          REAL,
            currency        TEXT,
            status          TEXT DEFAULT 'pending',  -- pending/paid/expired
            created_at      TEXT,
            paid_at         TEXT,
            processor_ref   TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id     INTEGER,
            pair            TEXT,
            direction       TEXT,
            zone            TEXT,
            result          TEXT,
            exit_reason     TEXT,
            rr              REAL,
            pnl_usd         REAL,
            pnl_pct         REAL,
            tps_hit         INTEGER,
            duration_min    INTEGER,
            open_time       TEXT,
            close_time      TEXT,
            data            TEXT                     -- JSON полного журнала
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(telegram_id);
        CREATE INDEX IF NOT EXISTS idx_pay_status  ON payments(status);
        """)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))


# ── Настройки (key-value, админ-редактируемые) ───────────────────────────────
def get_setting(key: str, default=None):
    with _connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default


def get_int_setting(key: str, default: int = 0) -> int:
    v = get_setting(key)
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def set_setting(key: str, value):
    with _write_lock, _connect() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


# ── Пользователи ─────────────────────────────────────────────────────────────
def upsert_user(telegram_id: int, username: str = None) -> dict:
    """Создаёт пользователя при первом /start (или обновляет username)."""
    with _write_lock, _connect() as c:
        c.execute(
            "INSERT INTO users(telegram_id, username, joined_at, status) "
            "VALUES(?, ?, ?, 'active') "
            # Не затираем существующий username, если при вызове он не передан (None)
            "ON CONFLICT(telegram_id) DO UPDATE SET "
            "username=COALESCE(excluded.username, users.username)",
            (telegram_id, username, _iso(_now())))
    return get_user(telegram_id)


def get_user(telegram_id: int):
    with _connect() as c:
        row = c.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
    return dict(row) if row else None


def set_user_field(telegram_id: int, field: str, value):
    allowed = {'username', 'status', 'exchange', 'api_key_enc', 'api_secret_enc',
               'paused', 'trial_until', 'paid_until', 'last_reminded_at'}
    if field not in allowed:
        raise ValueError(f"Поле {field} нельзя менять")
    with _write_lock, _connect() as c:
        c.execute(f"UPDATE users SET {field}=? WHERE telegram_id=?", (value, telegram_id))


def set_user_keys(telegram_id: int, exchange: str, api_key_enc: str, api_secret_enc: str):
    with _write_lock, _connect() as c:
        c.execute(
            "UPDATE users SET exchange=?, api_key_enc=?, api_secret_enc=? WHERE telegram_id=?",
            (exchange, api_key_enc, api_secret_enc, telegram_id))


def clear_user_keys(telegram_id: int):
    with _write_lock, _connect() as c:
        c.execute(
            "UPDATE users SET exchange=NULL, api_key_enc=NULL, api_secret_enc=NULL WHERE telegram_id=?",
            (telegram_id,))


def list_users(status: str = None):
    with _connect() as c:
        if status:
            rows = c.execute("SELECT * FROM users WHERE status=?", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM users").fetchall()
    return [dict(r) for r in rows]


def has_keys(user: dict) -> bool:
    return bool(user and user.get('api_key_enc') and user.get('api_secret_enc'))


def is_active(user: dict) -> bool:
    """Подписка активна: статус active И (оплачено до now ИЛИ триал до now)."""
    if not user or user.get('status') != 'active':
        return False
    now = _now()
    paid  = _parse(user.get('paid_until'))
    trial = _parse(user.get('trial_until'))
    return (paid and paid > now) or (trial and trial > now)


def access_until(user: dict):
    """Самая поздняя дата доступа (max от paid_until/trial_until)."""
    dates = [d for d in (_parse(user.get('paid_until')), _parse(user.get('trial_until'))) if d]
    return max(dates) if dates else None


def list_active_users():
    return [u for u in list_users() if is_active(u) and has_keys(u)]


def start_trial(telegram_id: int, days: int = None):
    """Стартует триал при первом подключении (если ещё не было ни триала, ни оплаты)."""
    if days is None:
        days = get_int_setting('trial_days', 7)
    user = get_user(telegram_id)
    if user and (user.get('trial_until') or user.get('paid_until')):
        return  # триал уже выдавался — повторно не даём
    set_user_field(telegram_id, 'trial_until', _iso(_now() + timedelta(days=days)))


def extend_subscription(telegram_id: int, days: int):
    """Продлевает paid_until на N дней от текущего конца доступа (или от now)."""
    user = get_user(telegram_id)
    base = access_until(user) or _now()
    if base < _now():
        base = _now()
    set_user_field(telegram_id, 'paid_until', _iso(base + timedelta(days=days)))


# ── Платежи ──────────────────────────────────────────────────────────────────
def create_payment(invoice_id: str, telegram_id: int, amount: float, currency: str,
                   processor_ref: str = None):
    with _write_lock, _connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO payments(invoice_id, telegram_id, amount, currency, "
            "status, created_at, processor_ref) VALUES(?, ?, ?, ?, 'pending', ?, ?)",
            (invoice_id, telegram_id, amount, currency, _iso(_now()), processor_ref))


def get_payment(invoice_id: str):
    with _connect() as c:
        row = c.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
    return dict(row) if row else None


def mark_payment_paid(invoice_id: str) -> bool:
    """Идемпотентно помечает платёж оплаченным. True если перешёл pending->paid (первый раз)."""
    with _write_lock, _connect() as c:
        row = c.execute("SELECT status FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not row or row['status'] == 'paid':
            return False
        c.execute("UPDATE payments SET status='paid', paid_at=? WHERE invoice_id=?",
                  (_iso(_now()), invoice_id))
        return True


def set_payment_status(invoice_id: str, status: str):
    """Принудительно меняет статус платежа (например, 'expired'/'failed' при опросе)."""
    with _write_lock, _connect() as c:
        c.execute("UPDATE payments SET status=? WHERE invoice_id=?", (status, invoice_id))


def list_pending_payments():
    with _connect() as c:
        rows = c.execute("SELECT * FROM payments WHERE status='pending'").fetchall()
    return [dict(r) for r in rows]


# ── Сделки (журнал per-user) ─────────────────────────────────────────────────
def record_trade(telegram_id: int, row: dict):
    with _write_lock, _connect() as c:
        c.execute(
            "INSERT INTO trades(telegram_id, pair, direction, zone, result, exit_reason, "
            "rr, pnl_usd, pnl_pct, tps_hit, duration_min, open_time, close_time, data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (telegram_id, row.get('pair'), row.get('direction'), row.get('zone'),
             row.get('result'), row.get('exit_reason'),
             _num(row.get('rr')), _num(row.get('pnl_usd')), _num(row.get('pnl_pct')),
             _int(row.get('tps_hit')), _int(row.get('duration_min')),
             row.get('open_time'), row.get('close_time'), json.dumps(row, default=str)))


def get_user_trades(telegram_id: int):
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE telegram_id=? ORDER BY id", (telegram_id,)).fetchall()
    return [dict(r) for r in rows]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None
