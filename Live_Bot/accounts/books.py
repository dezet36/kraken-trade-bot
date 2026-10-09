"""
Книги торговых счетов — общее для счёта «по инструкциям» (accounts/manual.py)
и биржевого (accounts/onexchange.py): реорганизация, этап 7.

Книга — что счёт держит и как живёт этап: заявки, позиции, кулдауны
стратегий, капитал на начало этапа и суток, статус этапа, журнал событий и
инструкций. Хранятся все книги в одном файле (trading_state.json в каталоге
данных, атомарная запись), закрытые сделки — в trading_trades.jsonl.

Здесь же — РЕШЕНИЕ СЧЁТА ПО СЕТАПУ (одно для обоих видов): стратегии счёта,
стороны, одна позиция на пару, пределы позиций и одной стороны, кулдаун и
предел издержек стратегии (strategy_profile), риск от капитала счёта и запас
правил пропа: худший исход открытых сделок вместе с новой (стопы плюс
издержки) не должен нарушить ни дневной убыток (от капитала на начало суток
UTC), ни макс. просадку (от начала этапа); оба предела — доля размера этапа.
Чем исполняется решённое — дело исполнителя: инструкцией владельцу или
заявкой на бирже.

Сообщения уходят через notify_with (ставит bot.py): счёт — слой ниже
интерфейсов и их не импортирует.
"""

import copy
import json
import os
import threading
from datetime import datetime, timezone

from logger import log

from execution import core

STATE_NAME = 'trading_state.json'
JOURNAL_NAME = 'trading_trades.jsonl'

BAR_TF = '5m'
BAR_MS = 5 * 60 * 1000
MAX_BARS, MAX_PAGES = 500, 12     # как у бумажного брокера: три недели простоя
KEEP_INSTRUCTIONS = 300

ACTIVE, TARGET, FAILED = 'active', 'target', 'failed'
STATUS_TEXT = {ACTIVE: 'торгует', TARGET: 'цель достигнута', FAILED: 'правила нарушены'}
WIN, LOSS, FLAT = 'плюс', 'минус', 'ноль'       # как в accounts/stats

ICONS = {'place': '📥', 'market': '⚡', 'cancel': '✖️', 'breakeven': '🛡', 'close': '⏱',
         'fill': '✅', 'target': '🎯', 'exit': '🏁', 'rules': '⛔', 'pass': '🏆', 'phase': '🔄',
         'order': '📤', 'error': '⚠️'}
EXIT_TEXT = {'SL': 'стоп', 'BE': 'безубыток', 'TIME': 'срок удержания',
             'MANUAL': 'закрыта владельцем', 'RULES': 'правила пропа', 'EXCHANGE': 'закрыта на бирже'}
DROP_TEXT = {'expired': 'срок заявки вышел', 'beyond': 'цена ушла за уровень до входа — импульс продолжился',
             'broken': 'сетап разрушен', 'target': 'цена дошла до цели без нас'}

lock = threading.RLock()
_cache = {'path': None, 'state': None}
_funding = {}          # пара -> (ставка за 8 ч, когда спрошена, мс)
_outbox = []           # сообщения, ещё не отправленные в Telegram
_notify = None         # куда слать: ставит bot.py (notify_with)


# ── Мелочи ──────────────────────────────────────────────────────────────────

def cfg():
    import config                    # заново: тесты перезагружают config
    return config


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec='seconds')


def when(ms):
    """Время для человека: «09.10 14:05 UTC»."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%d.%m %H:%M UTC')


def norm(symbol):
    return (symbol or '').replace('/', '').replace(':USDT', '').replace(':', '').upper()


def p(price):
    """Цена так, чтобы её можно было вбить в терминал: без пробелов."""
    price = float(price)
    if price >= 1000:
        return f'{price:.2f}'
    if price >= 1:
        return f'{price:.4f}'
    if price >= 0.01:
        return f'{price:.6f}'
    if price >= 0.0001:
        return f'{price:.8f}'
    return f'{price:.10f}'


def qty(size):
    """Объём в монетах: крупный — целым, мелкий — до четырёх значащих."""
    size = float(size)
    if size >= 1000:
        return f'{size:.0f}'
    if size >= 10:
        return f'{size:.1f}'
    if size >= 1:
        return f'{size:.3f}'
    return f'{size:.4g}'


def usd(amount, signed=False):
    text = f'${abs(float(amount)):,.2f}'.replace(',', ' ')
    if not signed:
        return text
    return ('+' if amount > 0 else '−' if amount < 0 else '') + text


def move(level, base):
    """Расстояние от входа в процентах, со знаком."""
    return (float(level) - float(base)) / float(base) * 100 if base else 0.0


def name(strategy):
    try:
        from strategies import registry
        entry = registry.get(strategy)
        return entry.title if entry else strategy
    except Exception:                              # noqa: BLE001
        return strategy


# ── Где лежит ───────────────────────────────────────────────────────────────

def state_path():
    return os.path.join(cfg().DATA_DIR, STATE_NAME)


def journal_path():
    return os.path.join(cfg().DATA_DIR, JOURNAL_NAME)


def state():
    """Книги всех торговых счетов: код счёта → книга."""
    path = state_path()
    if _cache['path'] != path or _cache['state'] is None:
        data = {}
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            # Нечитаемый файл не затирается: его место займёт новый, а сам он
            # останется рядом для разбора.
            spare = f"{path}.bad-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                os.replace(path, spare)
            except OSError:
                pass
            log(f'⚠️ {STATE_NAME} нечитаем ({exc}) — книги счетов начаты заново, прежний файл: {spare}')
        _cache['path'], _cache['state'] = path, (data if isinstance(data, dict) else {})
    return _cache['state']


def save():
    """Через временный файл и атомарную замену."""
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(_cache['state'], fh, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def append_row(row):
    """Закрытая сделка — строкой в журнал торговых счетов."""
    path = journal_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')


def read_journal():
    """Закрытые сделки всех торговых счетов (строка журнала — словарь)."""
    out = []
    try:
        with open(journal_path(), encoding='utf-8') as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue          # битая строка не прячет остальные
    except OSError:
        pass
    return out


# ── Счета и книги ───────────────────────────────────────────────────────────

def accounts(kind):
    """Торговые счета одного вида: код → правила (accounts/live.py)."""
    from accounts import live
    return {code: rules for code, rules in live.load().items() if rules['kind'] == kind}


def blank(rules, phase=1, now=None, start=None):
    """Пустая книга этапа. Размер этапа — start (биржа: капитал счёта) или
    размер из правил (проп по инструкциям)."""
    size = float(start if start is not None else (rules.get('deposit') or 0))
    return {'phase': phase, 'phase_started_at': iso(now or now_ms()),
            'start_balance': size, 'balance': size,
            'status': ACTIVE, 'status_at': None, 'status_why': '', 'day': None,
            'next_trade_id': 1, 'pending': {}, 'positions': {}, 'cooldown': {},
            'counts': {'offered': 0, 'orders': 0, 'refused': {}, 'dropped': {}},
            'instructions': [], 'next_instruction': 1}


def untouched(book):
    """В этапе ещё не было ни одной заявки."""
    return not book['pending'] and not book['positions'] and not book['counts']['orders']


def synced(book, rules):
    """Проп: пока в этапе не было заявок, размер счёта следует правилам (как
    депозит теста до первой сделки). У биржи размер — её капитал."""
    if rules.get('kind') != 'manual':
        return book
    deposit = float(rules.get('deposit') or 0)
    if untouched(book) and abs(deposit - book['start_balance']) >= 0.005:
        book['start_balance'] = book['balance'] = deposit
    return book


def get(code, rules, create=True, start=None):
    """Книга счёта (под замком lock). Нет — новая, если create."""
    books = state()
    book = books.get(code)
    if book is None:
        if not create:
            return None
        book = books[code] = blank(rules, start=start)
    return synced(book, rules)


def known_codes():
    """Коды счетов, у которых есть книга или сделки в журнале."""
    with lock:
        codes = set(state())
    return codes | {str(r.get('account')) for r in read_journal() if r.get('account')}


def busy(code):
    """Есть ли у счёта заявки или позиции в книге."""
    with lock:
        book = state().get(code)
        return bool(book and (book['pending'] or book['positions']))


def forget(code):
    """Книга удалённого счёта. Удаляется только пустая (см. accounts/live.remove)."""
    with lock:
        books = state()
        if code in books:
            if books[code]['pending'] or books[code]['positions']:
                raise ValueError('у счёта есть заявки или позиции — сначала закрыть')
            books.pop(code)
            save()


def open_for(strategy, rules, book):
    """Ждёт ли счёт сетапы стратегии: включён, выбрал её, этап не окончен."""
    return (rules['enabled'] and strategy in rules['strategies']
            and (book is None or book['status'] == ACTIVE))


# ── Деньги счёта ────────────────────────────────────────────────────────────

def unrealised(pos):
    """Плавающий итог позиции за вычетом уплаченного (как у бумажного брокера).
    У биржевой позиции уплаченное уже в балансе биржи — его поля нулевые."""
    price = pos.get('last_price') or pos['entry_price']
    sign = 1 if pos['direction'] == 'LONG' else -1
    return (pos.get('realized_pnl', 0.0) + sign * (price - pos['entry_price']) * pos['size']
            - pos.get('fees_paid', 0.0) - pos.get('funding_paid', 0.0))


def equity(book):
    return book['balance'] + sum(unrealised(x) for x in book['positions'].values())


def live_risk(pos):
    """Сколько позиция ещё может потерять по текущему стопу и остатку объёма."""
    entry, stop, size = pos.get('entry_price'), pos.get('stop_loss'), pos.get('size')
    if entry is None or stop is None or not size:
        return float(pos.get('risk_amount') or 0)
    long_ = pos['direction'] == 'LONG'
    loss = (entry - stop) if long_ else (stop - entry)
    return max(0.0, loss) * size


def open_risk(book):
    return (sum(live_risk(x) for x in book['positions'].values())
            + sum(float(o.get('risk_amount') or 0) for o in book['pending'].values()))


def day(book, now):
    """Капитал на начало суток UTC — запоминается при первом обращении в сутках
    и живёт в книге: перезапуск бота точку отсчёта не сбрасывает."""
    today = iso(now)[:10]
    if not book.get('day') or book['day'].get('date') != today:
        book['day'] = {'date': today, 'equity': round(equity(book), 2)}
    return book['day']


def room(rules, book, now):
    """
    Сколько ещё можно потерять, не нарушив правил пропа, если все открытые
    сделки и заявки дойдут до стопов: (сумма, какое правило ближе) или None —
    правил нет.
    """
    eq, risk, start = equity(book), open_risk(book), book['start_balance']
    rooms = []
    dd = float(rules.get('max_drawdown_pct') or 0)
    if dd > 0:
        rooms.append((eq - start * (1 - dd / 100) - risk, f'макс. просадка {dd:g}%'))
    daily = float(rules.get('daily_loss_pct') or 0)
    if daily > 0:
        used = max(0.0, day(book, now)['equity'] - eq)
        rooms.append((start * daily / 100 - used - risk, f'дневной убыток {daily:g}%'))
    return min(rooms) if rooms else None


# ── Сообщения ───────────────────────────────────────────────────────────────

def notify_with(func):
    """Куда слать сообщения счетов: func(имя счёта, сообщение). Ставит bot.py."""
    global _notify
    _notify = func


def instruct(code, rules, book, kind, title, lines, pair='', strategy='', action=True, now=None):
    """Инструкция (action — нужна рука владельца) или событие счёта: в книгу,
    в журнал бота и в очередь Telegram."""
    item = {'id': book['next_instruction'], 'account': code, 'at': iso(now or now_ms()),
            'kind': kind, 'icon': ICONS.get(kind, '📋'), 'title': title,
            'lines': [str(x) for x in lines], 'pair': pair, 'strategy': strategy,
            'action': bool(action), 'done': None}
    book['next_instruction'] += 1
    book['instructions'].append(item)
    del book['instructions'][:-KEEP_INSTRUCTIONS]
    who = rules.get('name') or code
    log(f"📋 [{who}] {title}: " + ' | '.join(item['lines']))
    _outbox.append((who, item))
    return item


def flush():
    """Отправка накопленного — вне замка: сеть не держит панель и цикл."""
    with lock:
        items = list(_outbox)
        _outbox.clear()
    if _notify is None:
        return
    for who, item in items:
        try:
            _notify(who, item)
        except Exception as exc:                   # noqa: BLE001
            log(f'⚠️ сообщение счёта «{who}» не отправлено: {exc}')


def plan_lines(order, entry=None):
    """Стоп, тейки, объём и риск — строки сообщения о входе."""
    entry = float(entry if entry is not None else order['limit_price'])
    lines = [f"Стоп: {p(order['stop_loss'])} ({move(order['stop_loss'], entry):+.2f}%)"]
    targets, fractions = order['targets'], order['fractions']
    if len(targets) == 1:
        lines.append(f"Тейк: {p(targets[0])} ({move(targets[0], entry):+.2f}%)")
    else:
        for k, level in enumerate(targets):
            lines.append(f"Тейк {k + 1}: {p(level)} ({move(level, entry):+.2f}%) — "
                         f"закрыть {float(fractions[k]) * 100:.0f}%")
    lines.append(f"Объём: {qty(order['size'])} (≈{usd(order['size'] * entry)})")
    share = order['risk_amount'] / order['balance_before'] * 100 if order.get('balance_before') else 0.0
    lines.append(f"Риск: {usd(order['risk_amount'])} — {share:.2f}% счёта")
    return lines


def ack(book, item, now):
    """Инструкция исполнена (номер item)."""
    for it in book['instructions']:
        if str(it['id']) == str(item):
            if not it.get('done'):
                it['done'] = iso(now)
                save()
            return 'отмечено'
    raise ValueError('инструкция не найдена')


# ── Сетап → решение ─────────────────────────────────────────────────────────

def setup_copy(signal):
    """Сетап для торговых счетов: что нужно решению и исполнению, без денег
    тестового счёта."""
    from strategies import contract
    params = {k: copy.deepcopy(v) for k, v in (signal.get('params') or {}).items()
              if k not in contract.MONEY_KEYS}
    return {'trading_pair': signal.get('trading_pair'),
            'setup': {'type': (signal.get('setup') or {}).get('type')},
            'params': params,
            'trigger': {'entry_type': (signal.get('trigger') or {}).get('entry_type')},
            'market_price': signal.get('market_price')}


def refuse(book, gate, detail=''):
    book['counts']['refused'][gate] = book['counts']['refused'].get(gate, 0) + 1
    return f'{gate}: {detail}' if detail else gate


def decide(rules, book, strategy, setup, now):
    """
    Решение счёта по сетапу: (заявка, None) или (None, причина отказа).
    Заявка — план входа на деньгах счёта; в книгу её кладёт register.
    """
    import risk_gate
    import strategy_profile
    from exit_plan import cooldown_hours, tp_plan, wants_breakeven
    c = cfg()
    book['counts']['offered'] += 1
    pair = norm(setup.get('trading_pair'))
    params = setup.get('params') or {}
    direction = (setup.get('setup') or {}).get('type')

    if book['status'] != ACTIVE:
        return None, refuse(book, 'этап завершён', STATUS_TEXT[book['status']])
    if direction not in ('LONG', 'SHORT') or not pair:
        return None, refuse(book, 'сетап неполный')
    if rules['sides'] != 'both' and rules['sides'] != direction.lower():
        return None, refuse(book, 'сторона', 'на счёте только '
                            + ('лонги' if rules['sides'] == 'long' else 'шорты'))
    # Один инструмент — одна позиция, как на настоящем счёте.
    if pair in book['pending'] or pair in book['positions']:
        return None, refuse(book, 'пара занята')
    last = (book['cooldown'].get(strategy) or {}).get(pair)
    if last and (now - int(last)) / 3_600_000 < cooldown_hours(strategy):
        return None, refuse(book, 'кулдаун')
    used = len(book['pending']) + len(book['positions'])
    if rules['max_slots'] and used >= rules['max_slots']:
        return None, refuse(book, 'позиций', f"{used}/{rules['max_slots']}")
    cap = rules['max_same_direction']
    same = sum(1 for shelf in (book['pending'], book['positions'])
               for item in shelf.values() if item['direction'] == direction)
    if cap and same >= cap:
        return None, refuse(book, 'в одну сторону', f'{direction} {same}/{cap}')

    try:
        entry, stop = float(params['entry']), float(params['stop_loss'])
    except (KeyError, TypeError, ValueError):
        return None, refuse(book, 'сетап неполный')
    long_ = direction == 'LONG'
    limit = core.limit_price(entry, long_, strategy_profile.limit_offset_pct(strategy))
    if (stop >= limit) if long_ else (stop <= limit):
        return None, refuse(book, 'стоп не по ту сторону входа')
    sl_dist = abs(limit - stop)
    pricey, cost_share, why = risk_gate.cost_too_high(
        limit, sl_dist, c.ENTRY_COST_ROUND_TRIP, strategy_profile.cost_limit_pct(strategy))
    if pricey:
        return None, refuse(book, 'предел издержек', why)
    balance = book['balance']
    if balance <= 0:
        return None, refuse(book, 'счёт пуст')
    size, risk_amount = core.position_size(balance, float(rules['risk_pct']), sl_dist,
                                           c.MAX_POSITION_SIZE_UNITS)
    if size * limit > balance * c.LEVERAGE:
        return None, refuse(book, 'плечо', f'объём {usd(size * limit)} > счёт×{c.LEVERAGE}')
    space = room(rules, book, now)
    worst = risk_amount * (1 + cost_share / 100)      # стоп плюс издержки круга
    if space is not None and worst > space[0]:
        return None, refuse(book, 'правила пропа',
                            f'{space[1]}: свободно {usd(max(space[0], 0))}, сделка рискует {usd(worst)}')
    targets, fractions = tp_plan(params)
    if not targets:
        return None, refuse(book, 'нет целей')
    # Цели — от ближней к дальней (доли — по местам), как у бумажного брокера.
    targets = sorted(targets, reverse=not long_)
    return {
        'strategy': strategy, 'pair': pair, 'direction': direction,
        'planned_entry': entry, 'limit_price': limit, 'stop_loss': stop,
        'targets': targets, 'fractions': fractions,
        'be_level': params.get('be_level'), 'breakeven_after_tp': wants_breakeven(params),
        'max_hold_hours': params.get('max_hold_hours'),
        'risk_amount': risk_amount, 'size': size, 'rr': float(params.get('rr') or 0),
        'cost_share_pct': round(cost_share, 3),
        'invalidation': float(params.get('pending_invalidation') or stop),
        'cancel_beyond': params.get('cancel_beyond'),
        'entry_type': (setup.get('trigger') or {}).get('entry_type') or 'LIMIT',
        'placed_ts': now, 'placed_at': iso(now),
        'expires_ts': now + int(strategy_profile.expiry_hours(strategy) * 3_600_000),
        # Первой считается свеча, открывшаяся не раньше постановки (как в тесте).
        'last_ts': now - 1, 'balance_before': balance,
    }, None


def register(book, order, now):
    """Заявка — в книгу; кулдаун стратегии по паре — с постановки, как в тесте."""
    book['pending'][order['pair']] = order
    book['cooldown'].setdefault(order['strategy'], {})[order['pair']] = now
    book['counts']['orders'] += 1


def through_market(strategy, setup, order):
    """Цена рынка, если лимит уже за рынком и стратегия исполняет такой лимит
    сразу (strategy_profile.fills_through_market) — как у бумажного брокера."""
    import strategy_profile
    if not strategy_profile.fills_through_market(strategy) or core.stop_entry(order):
        return None
    try:
        market = float(setup.get('market_price') or 0)
    except (TypeError, ValueError):
        return None
    if market <= 0:
        return None
    limit = order['limit_price']
    through = (limit >= market) if order['direction'] == 'LONG' else (limit <= market)
    return market if through else None


# ── Позиция и сделка ────────────────────────────────────────────────────────

def position(book, order, ts, price, size, fees_paid=0.0):
    """Позиция из налившейся заявки (номер сделки — по книге)."""
    trade_id = book['next_trade_id']
    book['next_trade_id'] += 1
    return {
        'trade_id': trade_id, 'strategy': order['strategy'], 'pair': order['pair'],
        'direction': order['direction'], 'entry_price': price,
        'planned_entry': order['planned_entry'], 'size': size, 'initial_size': size,
        'stop_loss': order['stop_loss'], 'initial_stop': order['stop_loss'],
        'targets': order['targets'], 'fractions': order['fractions'],
        'be_level': order['be_level'], 'breakeven_after_tp': order.get('breakeven_after_tp', True),
        'max_hold_hours': order.get('max_hold_hours'),
        'risk_amount': order['risk_amount'], 'rr': order['rr'],
        'cost_share_pct': order.get('cost_share_pct', ''),
        'opened_ts': ts, 'opened_at': iso(ts), 'placed_ts': order['placed_ts'],
        # Свеча налива — она же первая свеча позиции: в ней уже может стоять стоп.
        'last_ts': ts - 1, 'last_price': price, 'tp_hit': 0, 'realized_pnl': 0.0,
        'fees_paid': fees_paid, 'funding_paid': 0.0, 'funding_ts': ts, 'breakeven_set': False,
        'mfe_price': price, 'mae_price': price, 'mfe_ts': ts, 'mae_ts': ts,
        'balance_before': order['balance_before'],
    }


def trade_row(code, book, pos, ts, exit_price, reason, gross, fees, funding, net, before, after, **extra):
    """Строка журнала закрытой сделки (её читают панель и accounts/stats)."""
    sl_dist = abs(pos['entry_price'] - pos['initial_stop'])
    sign = 1 if pos['direction'] == 'LONG' else -1
    r = net / pos['risk_amount'] if pos['risk_amount'] else 0.0
    return {
        'account': code, 'phase': book['phase'], 'trade_id': pos['trade_id'],
        'strategy': pos['strategy'], 'pair': pos['pair'], 'direction': pos['direction'],
        'opened_at': pos['opened_at'], 'closed_at': iso(ts),
        'entry_price': pos['entry_price'], 'planned_entry': pos['planned_entry'],
        'stop_loss': pos['initial_stop'], 'targets': list(pos['targets']),
        'exit_price': exit_price, 'exit_reason': reason, 'tps_hit': pos['tp_hit'],
        'risk_usd': round(pos['risk_amount'], 2), 'position_size': pos['initial_size'],
        'gross_pnl_usd': round(gross, 4), 'fees_usd': round(fees, 4), 'funding_usd': round(funding, 4),
        'pnl_usd': round(net, 4), 'pnl_r': round(r, 3),
        'balance_before': round(before, 2), 'balance_after': round(after, 2),
        'duration_min': int((ts - pos['opened_ts']) / 60000),
        'mfe_r': round(sign * (pos['mfe_price'] - pos['entry_price']) / sl_dist, 3) if sl_dist else '',
        'mae_r': round(sign * (pos['mae_price'] - pos['entry_price']) / sl_dist, 3) if sl_dist else '',
        'breakeven_set': pos['breakeven_set'], 'data_gap_min': int(pos.get('gap_ms', 0) / 60000),
        'outcome': WIN if net > 0 else (LOSS if net < 0 else FLAT),
        '_placed': pos['placed_ts'] / 1000, '_t': ts / 1000, **extra,
    }


def result_lines(book, pos, exit_price, net, after):
    r = net / pos['risk_amount'] if pos['risk_amount'] else 0.0
    return [f"{p(pos['entry_price'])} → {p(exit_price)} · итог {usd(net, signed=True)} ({r:+.2f}R)",
            f"Счёт {usd(after)} ({move(after, book['start_balance']):+.2f}% от начала этапа)"]


# ── Свечи ───────────────────────────────────────────────────────────────────

def bars(client, pair, since_ts, now):
    """Закрытые 5-минутные свечи новее since_ts — постранично, как у брокера."""
    from data import exchange
    out, cursor, pages = [], since_ts, 0
    while cursor + 2 * BAR_MS <= now and pages < MAX_PAGES:
        pages += 1
        try:
            raw = exchange.fetch_raw(client, pair, BAR_TF, cursor + 1, MAX_BARS)
        except Exception as exc:                   # noqa: BLE001
            log(f'   ⚠️ {pair}: свечи для торговых счетов недоступны — {exc}')
            break
        fresh = [c for c in (raw or []) if c[0] > cursor and c[0] + BAR_MS <= now]
        if not fresh:
            break
        out.extend(fresh)
        if fresh[-1][0] <= cursor:
            break
        cursor = fresh[-1][0]
    return out


def funding_rate(client, pair, now):
    """Ставка фандинга за 8 ч (кэш 4 ч); недоступна — типовая из настроек."""
    c = cfg()
    if not c.PAPER_FUNDING:
        return 0.0
    cached = _funding.get(pair)
    if cached and now - cached[1] < 4 * 3_600_000:
        return cached[0]
    rate = c.PAPER_FUNDING_RATE_8H
    try:
        value = (client.fetch_funding_rate(pair) or {}).get('fundingRate')
        if value is not None:
            rate = float(value)
    except Exception:                              # noqa: BLE001
        pass
    _funding[pair] = (rate, now)
    return rate


def since_by_pair(books_):
    """С какой свечи нужны данные по каждой паре заявок и позиций книг."""
    since = {}
    for book in books_:
        for shelf in ('pending', 'positions'):
            for pair, rec in book[shelf].items():
                since[pair] = min(since.get(pair, rec['last_ts']), rec['last_ts'])
    return since


# ── Правила пропа ───────────────────────────────────────────────────────────

def check_rules(code, rules, book, now, finish):
    """Цель и пределы по капиталу счёта; окончание этапа — finish(статус, почему)."""
    if book['status'] != ACTIVE:
        return
    start = book['start_balance']
    if start <= 0:
        return
    eq = equity(book)
    today = day(book, now)
    dd = float(rules.get('max_drawdown_pct') or 0)
    daily = float(rules.get('daily_loss_pct') or 0)
    target = float(rules.get('profit_target_pct') or 0)
    if dd and eq <= start * (1 - dd / 100):
        finish(FAILED, f'просадка {(1 - eq / start) * 100:.2f}% от начала этапа — предел {dd:g}%')
    elif daily and today['equity'] - eq >= start * daily / 100:
        finish(FAILED, f"убыток за сутки {usd(today['equity'] - eq)} — предел {daily:g}% "
                       f"({usd(start * daily / 100)})")
    elif target and eq >= start * (1 + target / 100):
        finish(TARGET, f'капитал {usd(eq)} — цель +{target:g}% ({usd(start * (1 + target / 100))})')


def end_phase(book, status, why, now):
    book['status'], book['status_at'], book['status_why'] = status, iso(now), why


def new_phase(code, rules, book, now, start=None):
    """Новый этап: пустая книга с переносом журнала сообщений, номеров и кулдаунов."""
    if book['pending'] or book['positions']:
        raise ValueError('сначала закройте позиции и снимите заявки')
    fresh = blank(rules, phase=book['phase'] + 1, now=now, start=start)
    for key in ('instructions', 'next_instruction', 'next_trade_id', 'cooldown'):
        fresh[key] = book[key]
    state()[code] = fresh
    instruct(code, rules, fresh, 'phase', f"Этап {fresh['phase']}: счёт {usd(fresh['start_balance'])}",
             [f"Прошлый этап: {usd(book['balance'])} "
              f"({move(book['balance'], book['start_balance']):+.2f}%)"
              + (f" — {STATUS_TEXT[book['status']]}" if book['status'] != ACTIVE else '') + '.'],
             action=False, now=now)
    save()
    return fresh


# ── Для панели ──────────────────────────────────────────────────────────────

def position_view(pair, pos):
    price = pos.get('last_price') or pos['entry_price']
    total = unrealised(pos)
    view = {'pair': pair, 'strategy': pos['strategy'], 'direction': pos['direction'],
            'entry': pos['entry_price'], 'price': price, 'stop': pos['stop_loss'],
            'initial_stop': pos['initial_stop'], 'targets': list(pos['targets']),
            'fractions': list(pos['fractions']), 'tp_hit': pos.get('tp_hit', 0),
            'size': pos['size'], 'initial_size': pos['initial_size'],
            'risk': round(pos['risk_amount'], 2), 'result': round(total, 2),
            'result_r': round(total / pos['risk_amount'], 2) if pos['risk_amount'] else 0.0,
            'breakeven': pos.get('breakeven_set', False), 'opened': pos['opened_at']}
    hold = pos.get('max_hold_hours')
    if hold:
        view['exit_by'] = iso(int(pos['opened_ts'] + float(hold) * 3_600_000))
    return view


def order_view(pair, order, now):
    price = order.get('last_price')
    limit = order['limit_price']
    return {'pair': pair, 'strategy': order['strategy'], 'direction': order['direction'],
            'entry': limit, 'stop': order['stop_loss'], 'targets': list(order['targets']),
            'fractions': list(order['fractions']), 'size': order['size'],
            'risk': round(order['risk_amount'], 2), 'price': price,
            'distance_pct': round(abs(limit - price) / price * 100, 2) if price else None,
            'stop_entry': core.stop_entry(order), 'placed': order['placed_at'],
            'expires': iso(order['expires_ts']),
            'expires_in_min': max(0, int((order['expires_ts'] - now) / 60000))}


def report(code, rules, now_ms_=None):
    """Книга счёта для панели: деньги этапа, правила пропа, что ждёт исполнения,
    позиции и заявки, итоги закрытых сделок."""
    from accounts import stats
    now = now_ms_ or now_ms()
    with lock:
        stored = state().get(code)
        book = synced(copy.deepcopy(stored) if stored else blank(rules, now=now), rules)
    start = book['start_balance']
    eq = equity(book)
    today = (book['day'] if (book.get('day') or {}).get('date') == iso(now)[:10]
             else {'date': iso(now)[:10], 'equity': round(eq, 2)})
    dd = float(rules.get('max_drawdown_pct') or 0)
    daily = float(rules.get('daily_loss_pct') or 0)
    target = float(rules.get('profit_target_pct') or 0)
    level = start * (1 + target / 100) if target else None
    space = room(rules, book, now)
    rows = [r for r in read_journal() if r.get('account') == code]
    phase_rows = [r for r in rows if r.get('phase') == book['phase']]
    return {
        'phase': book['phase'], 'phase_started_at': book['phase_started_at'],
        'status': book['status'], 'status_text': STATUS_TEXT[book['status']],
        'status_why': book['status_why'], 'status_at': book['status_at'],
        'start': round(start, 2), 'balance': round(book['balance'], 2), 'equity': round(eq, 2),
        'return_pct': round((eq / start - 1) * 100, 3) if start else None,
        'deposit_rules': float(rules.get('deposit') or 0),
        'day_pnl': round(eq - today['equity'], 2),
        'daily_limit': round(start * daily / 100, 2) if daily else None,
        'daily_used_pct': (round(max(0.0, today['equity'] - eq) / (start * daily / 100) * 100, 1)
                           if daily and start else None),
        'drawdown_pct': round(max(0.0, (1 - eq / start) * 100), 3) if start else 0.0,
        'drawdown_limit_pct': dd or None,
        'floor': round(start * (1 - dd / 100), 2) if dd else None,
        'target_pct': target or None, 'target_level': round(level, 2) if level else None,
        'target_progress': (round(min(max((eq - start) / (level - start), 0.0), 1.0), 4)
                            if level and level > start else None),
        'open_risk': round(open_risk(book), 2),
        'room': {'amount': round(space[0], 2), 'rule': space[1]} if space else None,
        'positions': [position_view(k, x) for k, x in sorted(book['positions'].items())],
        'pending': [order_view(k, x, now) for k, x in sorted(book['pending'].items())],
        'waiting': [it for it in reversed(book['instructions']) if it['action'] and not it.get('done')][:30],
        'instructions': list(reversed(book['instructions'][-30:])),
        'counts': book['counts'],
        'quality': stats.quality(phase_rows),
        'quality_all': stats.quality(rows) if len(rows) != len(phase_rows) else None,
        'recent': list(reversed(phase_rows[-8:])),
        'started': stored is not None,
    }
