"""
Торговые счета — реорганизация, этап 7 (решения владельца 08.10.2026).

Счёт на бирже (свой Bybit, BingX: демо или реал) или проп «по инструкциям»
(HashHedge: API нет — бот показывает, какие сделки открыть, владелец исполняет
руками). Работают РЯДОМ с тестовыми счетами стратегий (accounts/paper.py), а не
вместо них: у каждого свой набор стратегий и свои правила денег — риск на
сделку, стороны, пределы позиций, правила пропа (цель по доходу, макс.
просадка, дневной убыток).

Здесь — реестр: что за счёт и его правила (trading_accounts.json в каталоге
данных) и ключи биржи (secrets/exchange_keys.json там же). Ключи вводятся в
приложении, проверяются на бирже тем же способом, каким пойдёт бот, хранятся
только на сервере: в панель не возвращаются, в журнал и историю настроек не
пишутся. Правки правил счёта — в историю настроек.

Исполнение: проп по инструкциям — accounts/manual.py (этап 7, шаг 2), биржи —
accounts/onexchange.py (шаг 3, сначала демо); одна дверь — accounts/trading.py.
"""

import json
import os
import re
import threading
from datetime import datetime, timezone

from logger import log

FILE_NAME = 'trading_accounts.json'
KEYS_FILE = os.path.join('secrets', 'exchange_keys.json')

KINDS = {'exchange': 'биржа', 'manual': 'проп по инструкциям'}
EXCHANGES = ('bybit', 'bingx')
MODES = ('demo', 'live')
SIDES = ('both', 'long', 'short')

LIMITS = {
    'risk_pct': (0.05, 5.0),
    'deposit': (10.0, 10_000_000.0),
    'max_slots': (0, 60),
    'max_same_direction': (0, 60),
    'daily_loss_pct': (0.0, 50.0),
    'max_drawdown_pct': (0.0, 90.0),
    'profit_target_pct': (0.0, 1000.0),
}
_WHOLE = ('max_slots', 'max_same_direction')

DEFAULTS = {
    'name': '', 'kind': 'manual', 'exchange': 'bybit', 'mode': 'demo', 'enabled': False,
    'strategies': [], 'risk_pct': 1.0, 'sides': 'both', 'max_slots': 0, 'max_same_direction': 0,
    'deposit': 10_000.0, 'daily_loss_pct': 0.0, 'max_drawdown_pct': 0.0, 'profit_target_pct': 0.0,
    'draft': False,
}
# Новый проп-счёт: типичные правила оценочного этапа проп-компаний — ЧЕРНОВИК,
# пока владелец не сохранит свои (поле draft на панели: «проверьте правила»).
PROP_DRAFT = {'deposit': 10_000.0, 'risk_pct': 1.0, 'profit_target_pct': 10.0,
              'max_drawdown_pct': 10.0, 'daily_loss_pct': 5.0, 'draft': True}


def kind_defaults():
    """Правила нового счёта по виду: одни и для save, и для формы панели."""
    return {'manual': {**DEFAULTS, 'kind': 'manual', **PROP_DRAFT},
            'exchange': {**DEFAULTS, 'kind': 'exchange'}}

_lock = threading.RLock()
_cache = {'key': None, 'data': None}


# ── Где лежит ───────────────────────────────────────────────────────────────

def _data_dir():
    import config                    # заново: тесты перезагружают config
    return config.DATA_DIR


def path():
    return os.path.join(_data_dir(), FILE_NAME)


def keys_path():
    return os.path.join(_data_dir(), KEYS_FILE)


def _write_json(p, data, private=False):
    """Через временный файл и атомарную замену; ключи — только владельцу."""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, p)


def _read_json(p, default):
    try:
        with open(p, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


# ── Проверка полей ──────────────────────────────────────────────────────────

def allowed_strategies():
    """Стратегии, которые могут торговать на торговом счёте: те, что идут в
    боевой цикл (ИИ торгуется только на бумаге — strategies/registry.py)."""
    from strategies import registry
    return registry.live_codes()


def _clamp(field, value, fallback):
    low, high = LIMITS[field]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    if value != value:
        return fallback
    value = min(max(value, low), high)
    return int(value) if field in _WHOLE else value


def slug(text):
    """Код счёта из имени: латиница, цифры и дефис."""
    table = str.maketrans('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
                          'abvgdeejziiklmnoprstufhccss_y_eua')
    value = re.sub(r'[^a-z0-9]+', '-', str(text or '').lower().translate(table)).strip('-')
    return value[:40] or 'account'


def clean(changes, current=None):
    """Правила счёта с проверкой каждого поля поверх current (или умолчаний)."""
    out = dict(DEFAULTS if current is None else current)
    changes = changes if isinstance(changes, dict) else {}
    if 'name' in changes:
        out['name'] = str(changes.get('name') or '').strip()[:60]
    if changes.get('kind') in KINDS:
        out['kind'] = changes['kind']
    if str(changes.get('exchange') or '').lower() in EXCHANGES:
        out['exchange'] = str(changes['exchange']).lower()
    if str(changes.get('mode') or '').lower() in MODES:
        out['mode'] = str(changes['mode']).lower()
    if 'enabled' in changes:
        out['enabled'] = bool(changes['enabled'])
    if 'strategies' in changes:
        allowed = set(allowed_strategies())
        picked = changes.get('strategies') or []
        out['strategies'] = [s for s in dict.fromkeys(str(x).upper() for x in picked) if s in allowed]
    if str(changes.get('sides') or '').lower() in SIDES:
        out['sides'] = str(changes['sides']).lower()
    for field in LIMITS:
        if field in changes:
            out[field] = _clamp(field, changes[field], out[field])
    if 'draft' in changes:
        out['draft'] = bool(changes['draft'])
    return out


# ── Реестр ──────────────────────────────────────────────────────────────────

def load(force=False):
    """{код счёта: правила}. Файл перечитывается, только если изменился."""
    with _lock:
        p = path()
        try:
            key = (p, os.path.getmtime(p))
        except OSError:
            key = (p, None)
        if not force and _cache['data'] is not None and _cache['key'] == key:
            return _cache['data']
        stored = _read_json(p, {}) if key[1] is not None else {}
        data = {}
        for code, item in stored.items():
            if isinstance(item, dict):
                data[str(code)] = clean(item, {**DEFAULTS, 'created_at': item.get('created_at')})
        _cache['key'], _cache['data'] = key, data
        return data


def get(code):
    return load().get(code)


def _history(before, after):
    """В историю настроек — правила счетов (ключей там нет и быть не может).
    У удалённого счёта поля уходят в None: иначе удаление не оставило бы следа."""
    from accounts import paper
    gone = {k: dict.fromkeys(v) for k, v in before.items() if k not in after}
    paper.write_history({f'Счёт {k}': v for k, v in before.items()},
                        {f'Счёт {k}': v for k, v in {**gone, **after}.items()})


def save(changes):
    """
    Создаёт или правит счёт. changes['id'] — код существующего; без него —
    новый (код из имени). Возвращает (код, правила). ValueError — не годится.
    """
    changes = changes if isinstance(changes, dict) else {}
    with _lock:
        before = json.loads(json.dumps(load(force=True)))
        data = json.loads(json.dumps(before))
        code = str(changes.get('id') or '').strip()
        if code:
            if code not in data:
                raise ValueError(f'нет счёта «{code}»')
            account = clean(changes, data[code])
        else:
            kind = changes.get('kind') if changes.get('kind') in KINDS else DEFAULTS['kind']
            account = clean(changes, kind_defaults()[kind])
            if not account['name']:
                raise ValueError('у счёта нет имени')
            code = slug(account['name'])
            # Код удалённого счёта новому не достаётся: его сделки остались в
            # журнале и попали бы в статистику нового.
            from accounts import books
            used = set(data) | books.known_codes()
            stem, n = code, 2
            while code in used:
                code, n = f'{stem}-{n}', n + 1
            account['created_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        if account['kind'] == 'exchange' and account['enabled'] and not has_keys(code):
            raise ValueError('у счёта биржи нет ключей — сначала ключи, потом торговля')
        data[code] = account
        _write_json(path(), data)
        _cache['key'], _cache['data'] = None, None
    _history(before, data)
    log(f"💼 торговый счёт {code}: {KINDS[account['kind']]}"
        f"{' ' + account['exchange'] + ' ' + account['mode'] if account['kind'] == 'exchange' else ''}, "
        f"{'торгует' if account['enabled'] else 'выключен'}, риск {account['risk_pct']}%, "
        f"стратегии {', '.join(account['strategies']) or 'нет'}")
    return code, account


def remove(code):
    """Удаляет счёт и его ключи. Счёт с заявками или позициями в записи удалять
    нельзя — сначала закрыть: иначе запись потеряла бы открытые сделки."""
    from accounts import books
    with _lock:
        before = json.loads(json.dumps(load(force=True)))
        if code not in before:
            raise ValueError(f'нет счёта «{code}»')
        if books.busy(code):
            raise ValueError('у счёта есть заявки или позиции — сначала закройте их')
        data = {k: v for k, v in before.items() if k != code}
        _write_json(path(), data)
        _cache['key'], _cache['data'] = None, None
        drop_keys(code)
        books.forget(code)
    _history(before, data)
    log(f'💼 торговый счёт {code} удалён')


# ── Ключи биржи ─────────────────────────────────────────────────────────────

def has_keys(code):
    item = _read_json(keys_path(), {}).get(code)
    return bool(isinstance(item, dict) and item.get('key') and item.get('secret'))


def keys(code):
    """(ключ, секрет) счёта — только для исполнения на сервере; None — нет."""
    item = _read_json(keys_path(), {}).get(code)
    if isinstance(item, dict) and item.get('key') and item.get('secret'):
        return item['key'], item['secret']
    return None


def check_keys(code, key, secret):
    """Ключи на бирже — тем же способом и на том же адресе (демо/реал), каким
    потом пойдёт бот. (годятся, ошибка)."""
    account = get(code)
    if not account or account['kind'] != 'exchange':
        return False, 'ключи нужны только счёту биржи'
    from accounts import exchange_keys
    return exchange_keys.check_keys(account['exchange'], account['mode'].upper(), key, secret)


def set_keys(code, key, secret):
    """Сохраняет проверенные ключи счёта (файл — только владельцу)."""
    key, secret = str(key or '').strip(), str(secret or '').strip()
    if not key or not secret:
        raise ValueError('заполните оба поля')
    with _lock:
        stored = _read_json(keys_path(), {})
        stored[code] = {'key': key, 'secret': secret,
                        'saved_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        _write_json(keys_path(), stored, private=True)
    log(f'🔑 ключи торгового счёта {code} заменены оператором')


def drop_keys(code):
    with _lock:
        stored = _read_json(keys_path(), {})
        if code in stored:
            stored.pop(code)
            _write_json(keys_path(), stored, private=True)


def public(code, account):
    """Счёт для панели: правила и признак ключей — без самих ключей."""
    return {'id': code, **account, 'kind_title': KINDS.get(account['kind'], account['kind']),
            'has_keys': has_keys(code) if account['kind'] == 'exchange' else None}


def listing():
    return [public(code, account) for code, account in sorted(load().items())]

