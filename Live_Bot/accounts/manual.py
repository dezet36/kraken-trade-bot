"""
Счета «по инструкциям» — проп без API (HashHedge и подобные): реорганизация,
этап 7, шаг 2 (решения владельца 08.10.2026).

Такой счёт владелец ведёт руками, а бот решает за него то же, что за тестовый
счёт стратегии, — по правилам ЭТОГО счёта (accounts/live.py): какие сетапы
брать (стратегии счёта, стороны, пределы позиций), на каких деньгах (риск от
капитала счёта) и не нарушит ли сделка правила пропа. Взятый сетап становится
инструкцией: поставить заявку — пара, сторона, вход, стоп, тейки, объём.

Дальше счёт ведётся по тем же правилам, что тест: ядро исполнения
(execution/core.py) и величины стратегии (strategy_profile — срок заявки,
кулдаун, предел издержек, смещение лимита, срок удержания) на 5-минутных
свечах. Событие, где нужна рука (снять заявку, перенести стоп, закрыть по
рынку, проверить стоп после налива), — инструкция с отметкой «Готово» на
панели; остальное (тейк взят, сделка закрыта стопом) — сводка.

Книга счёта — запись ПЛАНА: что было бы на счёте, если инструкции исполнены
вовремя. Сделал иначе — снять заявку или закрыть позицию в записи кнопкой на
панели.

ПРАВИЛА ПРОПА. Новая сделка берётся, только если худший исход всех открытых
сделок вместе с ней (риск по стопам плюс издержки) не нарушит ни дневной
убыток, ни макс. просадку: правило пропа — граница, а не повод дойти до неё.
Просадка — от начала этапа, дневной убыток — от капитала на начало суток UTC,
оба предела — доля размера счёта этапа. Капитал дошёл до цели — закрыть всё,
этап пройден; предел пробит (разрыв цены, проскальзывание) — счёт провален.
Новые заявки после этого не ставятся до «нового этапа» (ап-скейл: размер —
из правил счёта).

ОТКУДА СЕТАПЫ. Из того же прохода стратегии, что идёт на тест (bot.py), —
копия без денег, до решения тестового счёта. Отдельного прогона сканеров ради
торговых счетов нет: сканеры SMCS и Фибо 12ч помечают обработанный бар, и
лишний прогон отнял бы сетап у теста. Поэтому стратегия торгуется здесь, пока
её сканирует тест (включена, есть место).

Сообщения в Telegram шлёт интерфейс: bot.py ставит notify_with. Счёт — слой
ниже интерфейсов и их не импортирует.
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
         'fill': '✅', 'target': '🎯', 'exit': '🏁', 'rules': '⛔', 'pass': '🏆', 'phase': '🔄'}
EXIT_TEXT = {'SL': 'стоп', 'BE': 'безубыток', 'TIME': 'срок удержания',
             'MANUAL': 'закрыта владельцем', 'RULES': 'правила пропа'}

_lock = threading.RLock()
_cache = {'path': None, 'state': None}
_funding = {}          # пара -> (ставка за 8 ч, когда спрошена, мс)
_outbox = []           # инструкции, ещё не отправленные в Telegram
_notify = None         # куда слать инструкции: ставит bot.py (notify_with)


# ── Мелочи ──────────────────────────────────────────────────────────────────

def _cfg():
    import config                    # заново: тесты перезагружают config
    return config


def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec='seconds')


def _when(ms):
    """Время для человека: «09.10 14:05 UTC»."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%d.%m %H:%M UTC')


def _norm(symbol):
    return (symbol or '').replace('/', '').replace(':USDT', '').replace(':', '').upper()


def _p(price):
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


def _qty(size):
    """Объём в монетах: крупный — целым, мелкий — до четырёх значащих."""
    size = float(size)
    if size >= 1000:
        return f'{size:.0f}'
    if size >= 10:
        return f'{size:.1f}'
    if size >= 1:
        return f'{size:.3f}'
    return f'{size:.4g}'


def _usd(amount, signed=False):
    text = f'${abs(float(amount)):,.2f}'.replace(',', ' ')
    if not signed:
        return text
    return ('+' if amount > 0 else '−' if amount < 0 else '') + text


def _move(level, base):
    """Расстояние от входа в процентах, со знаком."""
    return (float(level) - float(base)) / float(base) * 100 if base else 0.0


def _name(strategy):
    try:
        from strategies import registry
        entry = registry.get(strategy)
        return entry.title if entry else strategy
    except Exception:                              # noqa: BLE001
        return strategy


# ── Где лежит ───────────────────────────────────────────────────────────────

def state_path():
    return os.path.join(_cfg().DATA_DIR, STATE_NAME)


def journal_path():
    return os.path.join(_cfg().DATA_DIR, JOURNAL_NAME)


def _state():
    """Книги всех счетов по инструкциям: код счёта → книга."""
    p = state_path()
    if _cache['path'] != p or _cache['state'] is None:
        data = {}
        try:
            with open(p, encoding='utf-8') as fh:
                data = json.load(fh)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            # Нечитаемый файл не затирается: его место займёт новый, а сам он
            # останется рядом для разбора.
            spare = f"{p}.bad-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                os.replace(p, spare)
            except OSError:
                pass
            log(f'⚠️ {STATE_NAME} нечитаем ({exc}) — книги счетов начаты заново, прежний файл: {spare}')
        _cache['path'], _cache['state'] = p, (data if isinstance(data, dict) else {})
    return _cache['state']


def _save():
    """Через временный файл и атомарную замену."""
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(_cache['state'], fh, indent=1, ensure_ascii=False, default=str)
    os.replace(tmp, p)


def _append_row(row):
    """Закрытая сделка — строкой в журнал счетов по инструкциям."""
    p = journal_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')


def read_journal():
    """Закрытые сделки всех счетов по инструкциям (строка журнала — словарь)."""
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

def _accounts():
    """Счета по инструкциям: код → правила (accounts/live.py)."""
    from accounts import live
    return {code: rules for code, rules in live.load().items() if rules['kind'] == 'manual'}


def _blank(rules, phase=1, now=None):
    deposit = float(rules.get('deposit') or 0)
    return {'phase': phase, 'phase_started_at': _iso(now or _now_ms()),
            'start_balance': deposit, 'balance': deposit,
            'status': ACTIVE, 'status_at': None, 'status_why': '', 'day': None,
            'next_trade_id': 1, 'pending': {}, 'positions': {}, 'cooldown': {},
            'counts': {'offered': 0, 'orders': 0, 'refused': {}, 'dropped': {}},
            'instructions': [], 'next_instruction': 1}


def _untouched(book):
    """В этапе ещё не было ни одной заявки."""
    return not book['pending'] and not book['positions'] and not book['counts']['orders']


def _synced(book, rules):
    """Пока в этапе не было заявок, размер счёта следует правилам (как депозит
    теста до первой сделки)."""
    deposit = float(rules.get('deposit') or 0)
    if _untouched(book) and abs(deposit - book['start_balance']) >= 0.005:
        book['start_balance'] = book['balance'] = deposit
    return book


def _book(code, rules, create=True):
    state = _state()
    book = state.get(code)
    if book is None:
        if not create:
            return None
        book = state[code] = _blank(rules)
    return _synced(book, rules)


def known_codes():
    """Коды счетов, у которых есть книга или сделки в журнале."""
    with _lock:
        codes = set(_state())
    return codes | {str(r.get('account')) for r in read_journal() if r.get('account')}


def busy(code):
    """Есть ли у счёта заявки или позиции в записи."""
    with _lock:
        book = _state().get(code)
        return bool(book and (book['pending'] or book['positions']))


def forget(code):
    """Книга удалённого счёта. Удаляется только пустая (см. accounts/live.remove)."""
    with _lock:
        state = _state()
        if code in state:
            if state[code]['pending'] or state[code]['positions']:
                raise ValueError('у счёта есть заявки или позиции — сначала закрыть')
            state.pop(code)
            _save()


# ── Деньги счёта ────────────────────────────────────────────────────────────

def _unrealised(pos):
    """Плавающий итог позиции за вычетом уплаченного (как у бумажного брокера)."""
    price = pos.get('last_price') or pos['entry_price']
    sign = 1 if pos['direction'] == 'LONG' else -1
    return (pos.get('realized_pnl', 0.0) + sign * (price - pos['entry_price']) * pos['size']
            - pos.get('fees_paid', 0.0) - pos.get('funding_paid', 0.0))


def equity(book):
    return book['balance'] + sum(_unrealised(p) for p in book['positions'].values())


def _live_risk(pos):
    """Сколько позиция ещё может потерять по текущему стопу и остатку объёма."""
    entry, stop, size = pos.get('entry_price'), pos.get('stop_loss'), pos.get('size')
    if entry is None or stop is None or not size:
        return float(pos.get('risk_amount') or 0)
    long_ = pos['direction'] == 'LONG'
    loss = (entry - stop) if long_ else (stop - entry)
    return max(0.0, loss) * size


def open_risk(book):
    return (sum(_live_risk(p) for p in book['positions'].values())
            + sum(float(o.get('risk_amount') or 0) for o in book['pending'].values()))


def _day(book, now):
    """Капитал на начало суток UTC — запоминается при первом обращении в сутках
    и живёт в книге: перезапуск бота точку отсчёта не сбрасывает."""
    today = _iso(now)[:10]
    if not book.get('day') or book['day'].get('date') != today:
        book['day'] = {'date': today, 'equity': round(equity(book), 2)}
    return book['day']


def _room(rules, book, now):
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
        used = max(0.0, _day(book, now)['equity'] - eq)
        rooms.append((start * daily / 100 - used - risk, f'дневной убыток {daily:g}%'))
    return min(rooms) if rooms else None


# ── Инструкции ──────────────────────────────────────────────────────────────

def notify_with(func):
    """Куда слать инструкции: func(имя счёта, инструкция). Ставит bot.py."""
    global _notify
    _notify = func


def _instruct(code, rules, book, kind, title, lines, pair='', strategy='', action=True, now=None):
    item = {'id': book['next_instruction'], 'account': code, 'at': _iso(now or _now_ms()),
            'kind': kind, 'icon': ICONS.get(kind, '📋'), 'title': title,
            'lines': [str(x) for x in lines], 'pair': pair, 'strategy': strategy,
            'action': bool(action), 'done': None}
    book['next_instruction'] += 1
    book['instructions'].append(item)
    del book['instructions'][:-KEEP_INSTRUCTIONS]
    name = rules.get('name') or code
    log(f"📋 [{name}] {title}: " + ' | '.join(item['lines']))
    _outbox.append((name, item))
    return item


def _flush():
    """Отправка накопленного — вне замка: сеть не держит панель и цикл."""
    with _lock:
        items = list(_outbox)
        _outbox.clear()
    if _notify is None:
        return
    for name, item in items:
        try:
            _notify(name, item)
        except Exception as exc:                   # noqa: BLE001
            log(f'⚠️ инструкция счёта «{name}» не отправлена: {exc}')


def _plan_lines(order, rules, entry=None):
    """Стоп, тейки, объём и риск — строки инструкции о входе."""
    entry = float(entry if entry is not None else order['limit_price'])
    lines = [f"Стоп: {_p(order['stop_loss'])} ({_move(order['stop_loss'], entry):+.2f}%)"]
    targets, fractions = order['targets'], order['fractions']
    if len(targets) == 1:
        lines.append(f"Тейк: {_p(targets[0])} ({_move(targets[0], entry):+.2f}%)")
    else:
        for k, level in enumerate(targets):
            lines.append(f"Тейк {k + 1}: {_p(level)} ({_move(level, entry):+.2f}%) — "
                         f"закрыть {float(fractions[k]) * 100:.0f}%")
    lines.append(f"Объём: {_qty(order['size'])} (≈{_usd(order['size'] * entry)})")
    share = order['risk_amount'] / order['balance_before'] * 100 if order.get('balance_before') else 0.0
    lines.append(f"Риск: {_usd(order['risk_amount'])} — {share:.2f}% счёта")
    return lines


# ── Сетап → заявка ──────────────────────────────────────────────────────────

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


def _open_for(strategy, rules, book):
    """Ждёт ли счёт сетапы стратегии: включён, выбрал её, этап не окончен."""
    return (rules['enabled'] and strategy in rules['strategies']
            and (book is None or book['status'] == ACTIVE))


def wants(strategy):
    """Ждёт ли сетапы этой стратегии хоть один счёт по инструкциям."""
    with _lock:
        state = _state()
        return any(_open_for(strategy, rules, state.get(code)) for code, rules in _accounts().items())


def offer(strategy, setup, now_ms=None):
    """
    Сетап — каждому включённому счёту по инструкциям, выбравшему стратегию.
    Возвращает [(счёт, None — взят | причина отказа)].
    """
    now = now_ms or _now_ms()
    out = []
    for code, rules in sorted(_accounts().items()):
        with _lock:
            if not _open_for(strategy, rules, _state().get(code)):
                continue
            book = _book(code, rules)
            why = _place(code, rules, book, strategy, copy.deepcopy(setup), now)
            _save()
        if why:
            log(f"   📋 [{rules.get('name') or code}] {strategy} "
                f"{_norm(setup.get('trading_pair'))}: не взят — {why}")
        out.append((code, why))
    _flush()
    return out


def _refuse(book, gate, detail=''):
    book['counts']['refused'][gate] = book['counts']['refused'].get(gate, 0) + 1
    return f'{gate}: {detail}' if detail else gate


def _place(code, rules, book, strategy, setup, now):
    """Решение счёта по сетапу и заявка. None — поставлена, иначе причина отказа."""
    import risk_gate
    import strategy_profile
    from exit_plan import cooldown_hours, tp_plan, wants_breakeven
    cfg = _cfg()
    book['counts']['offered'] += 1
    pair = _norm(setup.get('trading_pair'))
    params = setup.get('params') or {}
    direction = (setup.get('setup') or {}).get('type')

    if book['status'] != ACTIVE:
        return _refuse(book, 'этап завершён', STATUS_TEXT[book['status']])
    if direction not in ('LONG', 'SHORT') or not pair:
        return _refuse(book, 'сетап неполный')
    if rules['sides'] != 'both' and rules['sides'] != direction.lower():
        return _refuse(book, 'сторона', 'на счёте только '
                       + ('лонги' if rules['sides'] == 'long' else 'шорты'))
    # Один инструмент — одна позиция, как на настоящем счёте.
    if pair in book['pending'] or pair in book['positions']:
        return _refuse(book, 'пара занята')
    last = (book['cooldown'].get(strategy) or {}).get(pair)
    if last and (now - int(last)) / 3_600_000 < cooldown_hours(strategy):
        return _refuse(book, 'кулдаун')
    used = len(book['pending']) + len(book['positions'])
    if rules['max_slots'] and used >= rules['max_slots']:
        return _refuse(book, 'позиций', f"{used}/{rules['max_slots']}")
    cap = rules['max_same_direction']
    same = sum(1 for shelf in (book['pending'], book['positions'])
               for item in shelf.values() if item['direction'] == direction)
    if cap and same >= cap:
        return _refuse(book, 'в одну сторону', f'{direction} {same}/{cap}')

    try:
        entry, stop = float(params['entry']), float(params['stop_loss'])
    except (KeyError, TypeError, ValueError):
        return _refuse(book, 'сетап неполный')
    long_ = direction == 'LONG'
    limit = core.limit_price(entry, long_, strategy_profile.limit_offset_pct(strategy))
    if (stop >= limit) if long_ else (stop <= limit):
        return _refuse(book, 'стоп не по ту сторону входа')
    sl_dist = abs(limit - stop)
    pricey, cost_share, why = risk_gate.cost_too_high(
        limit, sl_dist, cfg.ENTRY_COST_ROUND_TRIP, strategy_profile.cost_limit_pct(strategy))
    if pricey:
        return _refuse(book, 'предел издержек', why)
    balance = book['balance']
    if balance <= 0:
        return _refuse(book, 'счёт пуст')
    size, risk_amount = core.position_size(balance, float(rules['risk_pct']), sl_dist,
                                           cfg.MAX_POSITION_SIZE_UNITS)
    if size * limit > balance * cfg.LEVERAGE:
        return _refuse(book, 'плечо', f'объём {_usd(size * limit)} > счёт×{cfg.LEVERAGE}')
    room = _room(rules, book, now)
    worst = risk_amount * (1 + cost_share / 100)      # стоп плюс издержки круга
    if room is not None and worst > room[0]:
        return _refuse(book, 'правила пропа',
                       f'{room[1]}: свободно {_usd(max(room[0], 0))}, сделка рискует {_usd(worst)}')
    targets, fractions = tp_plan(params)
    if not targets:
        return _refuse(book, 'нет целей')
    # Цели — от ближней к дальней (доли — по местам), как у бумажного брокера.
    targets = sorted(targets, reverse=not long_)

    order = {
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
        'placed_ts': now, 'placed_at': _iso(now),
        'expires_ts': now + int(strategy_profile.expiry_hours(strategy) * 3_600_000),
        # Первой считается свеча, открывшаяся не раньше постановки (как в тесте).
        'last_ts': now - 1, 'balance_before': balance,
    }
    book['pending'][pair] = order
    book['cooldown'].setdefault(strategy, {})[pair] = now
    book['counts']['orders'] += 1

    head = f"{pair} {direction}"
    market = _through_market(strategy, setup, order)
    if market is not None:
        price = core.through_market_price(order, market, cfg.PAPER_SLIPPAGE_PCT)
        _instruct(code, rules, book, 'market', f'Войти по рынку · {head}',
                  [f'Стратегия: {_name(strategy)}',
                   f'Цена ≈ {_p(price)} — лимит {_p(limit)} уже за рынком'] + _plan_lines(order, rules, price),
                  pair, strategy, now=now)
        _fill(code, rules, book, pair, order, now, price, taker=True, quiet=True)
        return None
    kind = 'Стоп-заявка на вход (по ходу движения)' if core.stop_entry(order) else 'Лимит на вход'
    _instruct(code, rules, book, 'place', f'Поставить заявку · {head}',
              [f'Стратегия: {_name(strategy)}', f'{kind}: {_p(limit)}'] + _plan_lines(order, rules)
              + [f"Не налилась до {_when(order['expires_ts'])} — снять (напомню)."],
              pair, strategy, now=now)
    return None


def _through_market(strategy, setup, order):
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


# ── Ведение по свечам ───────────────────────────────────────────────────────

def _fill(code, rules, book, pair, order, ts, price, taker=False, quiet=False):
    cfg = _cfg()
    book['pending'].pop(pair, None)
    size = order['size']
    trade_id = book['next_trade_id']
    book['next_trade_id'] += 1
    pos = {
        'trade_id': trade_id, 'strategy': order['strategy'], 'pair': pair,
        'direction': order['direction'], 'entry_price': price,
        'planned_entry': order['planned_entry'], 'size': size, 'initial_size': size,
        'stop_loss': order['stop_loss'], 'initial_stop': order['stop_loss'],
        'targets': order['targets'], 'fractions': order['fractions'],
        'be_level': order['be_level'], 'breakeven_after_tp': order.get('breakeven_after_tp', True),
        'max_hold_hours': order.get('max_hold_hours'),
        'risk_amount': order['risk_amount'], 'rr': order['rr'],
        'cost_share_pct': order.get('cost_share_pct', ''),
        'opened_ts': ts, 'opened_at': _iso(ts), 'placed_ts': order['placed_ts'],
        # Свеча налива — она же первая свеча позиции: в ней уже может стоять стоп.
        'last_ts': ts - 1, 'last_price': price, 'tp_hit': 0, 'realized_pnl': 0.0,
        'fees_paid': core.entry_fee(size, price, taker, cfg.PAPER_FEE_MAKER, cfg.PAPER_FEE_TAKER),
        'funding_paid': 0.0, 'funding_ts': ts, 'breakeven_set': False,
        'mfe_price': price, 'mae_price': price, 'mfe_ts': ts, 'mae_ts': ts,
        'balance_before': order['balance_before'],
    }
    book['positions'][pair] = pos
    if not quiet:
        _instruct(code, rules, book, 'fill', f"Заявка налилась · {pair} {pos['direction']}",
                  [f'Вход {_p(price)}. Проверьте, что стоят стоп {_p(pos["stop_loss"])} и тейк'
                   + ('и' if len(pos['targets']) > 1 else '') + ': '
                   + ', '.join(_p(t) for t in pos['targets'])],
                  pair, pos['strategy'], now=ts)


DROP_TEXT = {'expired': 'срок заявки вышел', 'beyond': 'цена ушла за уровень до входа — импульс продолжился',
             'broken': 'сетап разрушен', 'target': 'цена дошла до цели без нас'}


def _drop(code, rules, book, pair, reason, ts, quiet=False):
    order = book['pending'].pop(pair, None)
    if not order:
        return
    dropped = book['counts']['dropped']
    dropped[reason] = dropped.get(reason, 0) + 1
    if not quiet:
        _instruct(code, rules, book, 'cancel', f"Снять заявку · {pair} {order['direction']}",
                  [f"Вход {_p(order['limit_price'])} — {reason}."], pair, order['strategy'], now=ts)


def _close(code, rules, book, pair, pos, ts, price, reason, slip, quiet=False):
    cfg = _cfg()
    exit_price, gross, fees, funding, net = core.close_numbers(
        pos, price, reason, slip, cfg.PAPER_SLIPPAGE_PCT, cfg.PAPER_FEE_MAKER, cfg.PAPER_FEE_TAKER)
    before = book['balance']
    after = before + net
    book['balance'] = after
    book['positions'].pop(pair, None)
    book['cooldown'].setdefault(pos['strategy'], {})[pair] = ts
    sl_dist = abs(pos['entry_price'] - pos['initial_stop'])
    sign = 1 if pos['direction'] == 'LONG' else -1
    r = net / pos['risk_amount'] if pos['risk_amount'] else 0.0
    row = {
        'account': code, 'phase': book['phase'], 'trade_id': pos['trade_id'],
        'strategy': pos['strategy'], 'pair': pair, 'direction': pos['direction'],
        'opened_at': pos['opened_at'], 'closed_at': _iso(ts),
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
        '_placed': pos['placed_ts'] / 1000, '_t': ts / 1000,
    }
    _append_row(row)
    if quiet:
        return row
    head = f"{pair} {pos['direction']}"
    result = [f"{_p(pos['entry_price'])} → {_p(exit_price)} · итог {_usd(net, signed=True)} ({r:+.2f}R)",
              f"Счёт {_usd(after)} ({_move(after, book['start_balance']):+.2f}% от начала этапа)"]
    if reason == 'TIME':
        import strategy_profile
        hold = pos.get('max_hold_hours') or strategy_profile.max_hold_hours(pos['strategy'])
        _instruct(code, rules, book, 'close', f'Закрыть по рынку · {head}',
                  [f"Срок удержания{' ' + format(float(hold), 'g') + ' ч' if hold else ''} вышел — "
                   f"закрыть остаток по рынку."] + result, pair, pos['strategy'], now=ts)
    else:
        what = (f"тейк {reason[2:]}" if reason.startswith('TP') else EXIT_TEXT.get(reason, reason))
        _instruct(code, rules, book, 'exit', f'Сделка закрыта · {head} · {what}', result,
                  pair, pos['strategy'], action=False, now=ts)
    return row


def _step(code, rules, book, pair, bar, funding_rate):
    """Одна закрытая 5-минутная свеча для заявки и позиции счёта по паре."""
    import strategy_profile
    cfg = _cfg()
    ts, open_, high, low, close = bar[0], bar[1], bar[2], bar[3], bar[4]

    order = book['pending'].get(pair)
    if order and ts > order['last_ts']:
        order['last_ts'], order['last_price'] = ts, close
        what, detail = core.simulate_pending(
            order, ts, high, low, open_,
            drops_at_target=strategy_profile.drops_at_target(order['strategy']))
        if what == 'fill':
            _fill(code, rules, book, pair, order, ts, detail, taker=core.stop_entry(order))
        elif what:
            _drop(code, rules, book, pair, DROP_TEXT[what], ts)

    pos = book['positions'].get(pair)
    if not pos or ts <= pos['last_ts']:
        return
    # Минуты без свечей копятся на позиции: сделка через дыру в данных видна
    # по одной колонке журнала (как у бумажного брокера).
    skipped = ts - pos['last_ts'] - BAR_MS
    if skipped > 0:
        pos['gap_ms'] = pos.get('gap_ms', 0) + skipped
    pos['last_ts'], pos['last_price'] = ts, close
    core.apply_funding(pos, ts, funding_rate)
    max_hold = pos.get('max_hold_hours') or strategy_profile.max_hold_hours(pos['strategy'])
    head = f"{pair} {pos['direction']}"
    was_be = pos['breakeven_set']
    for what, detail in core.simulate_position(pos, ts, high, low, close, max_hold, cfg.PAPER_FEE_MAKER):
        if what == 'breakeven':
            _instruct(code, rules, book, 'breakeven', f'Стоп в безубыток · {head}',
                      [f"Цена прошла {_p(pos['be_level'])} — перенесите стоп на {_p(pos['stop_loss'])}."],
                      pair, pos['strategy'], now=ts)
        elif what == 'target':
            index, level = detail
            lines = [f"Тейк {index + 1} из {len(pos['targets'])}: {_p(level)} — "
                     f"закрыто {float(pos['fractions'][index]) * 100:.0f}%."]
            moved = pos['breakeven_set'] and not was_be
            if moved:
                lines.append(f"Перенесите стоп остатка в безубыток: {_p(pos['stop_loss'])}.")
            _instruct(code, rules, book, 'target', f'Тейк {index + 1} взят · {head}', lines,
                      pair, pos['strategy'], action=moved, now=ts)
        elif what == 'close':
            price, reason, slip = detail
            _close(code, rules, book, pair, pos, ts, price, reason, slip)
        was_be = pos.get('breakeven_set', was_be)


def _bars(client, pair, since_ts, now):
    """Закрытые 5-минутные свечи новее since_ts — постранично, как у брокера."""
    import exchange
    out, cursor, pages = [], since_ts, 0
    while cursor + 2 * BAR_MS <= now and pages < MAX_PAGES:
        pages += 1
        try:
            raw = exchange.fetch_raw(client, pair, BAR_TF, cursor + 1, MAX_BARS)
        except Exception as exc:                   # noqa: BLE001
            log(f'   ⚠️ {pair}: свечи для счетов по инструкциям недоступны — {exc}')
            break
        fresh = [c for c in (raw or []) if c[0] > cursor and c[0] + BAR_MS <= now]
        if not fresh:
            break
        out.extend(fresh)
        if fresh[-1][0] <= cursor:
            break
        cursor = fresh[-1][0]
    return out


def _funding_rate(client, pair, now):
    """Ставка фандинга за 8 ч (кэш 4 ч); недоступна — типовая из настроек."""
    cfg = _cfg()
    if not cfg.PAPER_FUNDING:
        return 0.0
    cached = _funding.get(pair)
    if cached and now - cached[1] < 4 * 3_600_000:
        return cached[0]
    rate = cfg.PAPER_FUNDING_RATE_8H
    try:
        value = (client.fetch_funding_rate(pair) or {}).get('fundingRate')
        if value is not None:
            rate = float(value)
    except Exception:                              # noqa: BLE001
        pass
    _funding[pair] = (rate, now)
    return rate


def update(client, now_ms=None):
    """Раз в цикл бота: свечи по парам счетов, события заявок и позиций,
    правила пропа. Ведётся и выключенный счёт: выключение запрещает новые
    заявки, а не ведение открытых."""
    now = now_ms or _now_ms()
    accounts = _accounts()
    with _lock:
        state = _state()
        since = {}
        for code in accounts:
            book = state.get(code)
            if not book:
                continue
            for shelf in ('pending', 'positions'):
                for pair, rec in book[shelf].items():
                    since[pair] = min(since.get(pair, rec['last_ts']), rec['last_ts'])
    # Сеть — вне замка: панель и Telegram не ждут свечей.
    bars = {pair: _bars(client, pair, start, now) for pair, start in since.items()}
    rates = {pair: _funding_rate(client, pair, now) for pair in since}
    with _lock:
        state = _state()
        touched = False
        for code, rules in sorted(accounts.items()):
            book = state.get(code)
            if not book:
                continue
            touched = True
            for pair in sorted(set(book['pending']) | set(book['positions'])):
                for bar in bars.get(pair) or []:
                    _step(code, rules, book, pair, bar, rates.get(pair, 0.0))
            _check_rules(code, rules, book, now)
        if touched:
            _save()
    _flush()


def _check_rules(code, rules, book, now):
    """Цель и пределы пропа по капиталу счёта."""
    if book['status'] != ACTIVE:
        return
    start = book['start_balance']
    if start <= 0:
        return
    eq = equity(book)
    day = _day(book, now)
    dd = float(rules.get('max_drawdown_pct') or 0)
    daily = float(rules.get('daily_loss_pct') or 0)
    target = float(rules.get('profit_target_pct') or 0)
    if dd and eq <= start * (1 - dd / 100):
        _finish(code, rules, book, FAILED,
                f'просадка {(1 - eq / start) * 100:.2f}% от начала этапа — предел {dd:g}%', now)
    elif daily and day['equity'] - eq >= start * daily / 100:
        _finish(code, rules, book, FAILED,
                f"убыток за сутки {_usd(day['equity'] - eq)} — предел {daily:g}% ({_usd(start * daily / 100)})", now)
    elif target and eq >= start * (1 + target / 100):
        _finish(code, rules, book, TARGET,
                f'капитал {_usd(eq)} — цель +{target:g}% ({_usd(start * (1 + target / 100))})', now)


def _finish(code, rules, book, status, why, now):
    """Этап окончен: закрыть позиции по рынку, снять заявки, новых не ставить."""
    lines = [why[0].upper() + why[1:] + '.']
    for pair in sorted(book['positions']):
        pos = book['positions'][pair]
        lines.append(f"Закрыть по рынку: {pair} {pos['direction']}")
        _close(code, rules, book, pair, pos, now, pos.get('last_price') or pos['entry_price'],
               'RULES', slip=True, quiet=True)
    for pair in sorted(book['pending']):
        order = book['pending'][pair]
        lines.append(f"Снять заявку: {pair} {order['direction']} {_p(order['limit_price'])}")
        _drop(code, rules, book, pair, 'правила пропа', now, quiet=True)
    acted = len(lines) > 1
    book['status'], book['status_at'], book['status_why'] = status, _iso(now), why
    lines.append(f"Счёт {_usd(book['balance'])} ({_move(book['balance'], book['start_balance']):+.2f}% за этап). "
                 'Новые заявки не ставятся; новый этап — кнопкой на странице «Счета».')
    if status == TARGET:
        _instruct(code, rules, book, 'pass', 'Цель пропа достигнута', lines, action=acted, now=now)
    else:
        _instruct(code, rules, book, 'rules', 'Правила пропа нарушены — торговля остановлена',
                  lines, action=acted, now=now)


# ── Действия владельца ──────────────────────────────────────────────────────

def act(code, action, item=None, pair=None, now_ms=None):
    """
    Действие владельца на панели. Возвращает текст ответа; ValueError — нельзя.
      ack        инструкция исполнена (item — её номер);
      cancel     заявка снята владельцем — убрать из записи (pair);
      close      позиция закрыта владельцем — по последней цене (pair);
      new_phase  новый этап с размером из правил (ап-скейл, перезапуск).
    """
    rules = _accounts().get(code)
    if rules is None:
        raise ValueError(f'нет счёта по инструкциям «{code}»')
    now = now_ms or _now_ms()
    with _lock:
        book = _book(code, rules, create=(action == 'new_phase'))
        if book is None:
            raise ValueError('по счёту ещё не было заявок')
        if action == 'ack':
            for it in book['instructions']:
                if str(it['id']) == str(item):
                    if not it.get('done'):
                        it['done'] = _iso(now)
                        _save()
                    return 'отмечено'
            raise ValueError('инструкция не найдена')
        if action == 'cancel':
            pair = _norm(pair)
            if pair not in book['pending']:
                raise ValueError(f'{pair}: заявки нет')
            _drop(code, rules, book, pair, 'снята владельцем', now, quiet=True)
            _save()
            return f'{pair}: заявка убрана из записи счёта'
        if action == 'close':
            pair = _norm(pair)
            pos = book['positions'].get(pair)
            if not pos:
                raise ValueError(f'{pair}: позиции нет')
            price = pos.get('last_price') or pos['entry_price']
            _close(code, rules, book, pair, pos, now, price, 'MANUAL', slip=True, quiet=True)
            _save()
            return f'{pair}: позиция закрыта в записи по {_p(price)}'
        if action == 'new_phase':
            if book['pending'] or book['positions']:
                raise ValueError('сначала закройте позиции и снимите заявки')
            fresh = _blank(rules, phase=book['phase'] + 1, now=now)
            for key in ('instructions', 'next_instruction', 'next_trade_id', 'cooldown'):
                fresh[key] = book[key]
            _state()[code] = fresh
            _instruct(code, rules, fresh, 'phase', f"Этап {fresh['phase']}: счёт {_usd(fresh['start_balance'])}",
                      [f"Прошлый этап: {_usd(book['balance'])} "
                       f"({_move(book['balance'], book['start_balance']):+.2f}%)"
                       + (f" — {STATUS_TEXT[book['status']]}" if book['status'] != ACTIVE else '') + '.'],
                      action=False, now=now)
            _save()
            reply = f"начат этап {fresh['phase']}"
        else:
            raise ValueError('неизвестное действие')
    _flush()
    return reply


# ── Для панели ──────────────────────────────────────────────────────────────

def _position_view(pair, pos):
    price = pos.get('last_price') or pos['entry_price']
    total = _unrealised(pos)
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
        view['exit_by'] = _iso(int(pos['opened_ts'] + float(hold) * 3_600_000))
    return view


def _order_view(pair, order, now):
    price = order.get('last_price')
    limit = order['limit_price']
    return {'pair': pair, 'strategy': order['strategy'], 'direction': order['direction'],
            'entry': limit, 'stop': order['stop_loss'], 'targets': list(order['targets']),
            'fractions': list(order['fractions']), 'size': order['size'],
            'risk': round(order['risk_amount'], 2), 'price': price,
            'distance_pct': round(abs(limit - price) / price * 100, 2) if price else None,
            'stop_entry': core.stop_entry(order), 'placed': order['placed_at'],
            'expires': _iso(order['expires_ts']),
            'expires_in_min': max(0, int((order['expires_ts'] - now) / 60000))}


def report(code, rules, now_ms=None):
    """Книга счёта для панели: деньги этапа, правила пропа, что ждёт исполнения,
    позиции и заявки, итоги закрытых сделок."""
    from accounts import stats
    now = now_ms or _now_ms()
    with _lock:
        stored = _state().get(code)
        book = _synced(copy.deepcopy(stored) if stored else _blank(rules, now=now), rules)
    start = book['start_balance']
    eq = equity(book)
    day = (book['day'] if (book.get('day') or {}).get('date') == _iso(now)[:10]
           else {'date': _iso(now)[:10], 'equity': round(eq, 2)})
    dd = float(rules.get('max_drawdown_pct') or 0)
    daily = float(rules.get('daily_loss_pct') or 0)
    target = float(rules.get('profit_target_pct') or 0)
    level = start * (1 + target / 100) if target else None
    room = _room(rules, book, now)
    rows = [r for r in read_journal() if r.get('account') == code]
    phase_rows = [r for r in rows if r.get('phase') == book['phase']]
    return {
        'phase': book['phase'], 'phase_started_at': book['phase_started_at'],
        'status': book['status'], 'status_text': STATUS_TEXT[book['status']],
        'status_why': book['status_why'], 'status_at': book['status_at'],
        'start': round(start, 2), 'balance': round(book['balance'], 2), 'equity': round(eq, 2),
        'return_pct': round((eq / start - 1) * 100, 3) if start else None,
        'deposit_rules': float(rules.get('deposit') or 0),
        'day_pnl': round(eq - day['equity'], 2),
        'daily_limit': round(start * daily / 100, 2) if daily else None,
        'daily_used_pct': (round(max(0.0, day['equity'] - eq) / (start * daily / 100) * 100, 1)
                           if daily and start else None),
        'drawdown_pct': round(max(0.0, (1 - eq / start) * 100), 3) if start else 0.0,
        'drawdown_limit_pct': dd or None,
        'floor': round(start * (1 - dd / 100), 2) if dd else None,
        'target_pct': target or None, 'target_level': round(level, 2) if level else None,
        'target_progress': (round(min(max((eq - start) / (level - start), 0.0), 1.0), 4)
                            if level and level > start else None),
        'open_risk': round(open_risk(book), 2),
        'room': {'amount': round(room[0], 2), 'rule': room[1]} if room else None,
        'positions': [_position_view(p, x) for p, x in sorted(book['positions'].items())],
        'pending': [_order_view(p, x, now) for p, x in sorted(book['pending'].items())],
        'waiting': [it for it in reversed(book['instructions']) if it['action'] and not it.get('done')][:30],
        'instructions': list(reversed(book['instructions'][-30:])),
        'counts': book['counts'],
        'quality': stats.quality(phase_rows),
        'quality_all': stats.quality(rows) if len(rows) != len(phase_rows) else None,
        'recent': list(reversed(phase_rows[-8:])),
        'started': stored is not None,
    }


def describe():
    """Строки для журнала запуска: какие счета по инструкциям ведутся."""
    out = []
    for code, rules in sorted(_accounts().items()):
        with _lock:
            book = _state().get(code)
        money = (f"этап {book['phase']}, счёт {_usd(book['balance'])} — {STATUS_TEXT[book['status']]}"
                 if book else f"счёт {_usd(rules.get('deposit') or 0)}, заявок ещё не было")
        out.append(f"   📋 {rules.get('name') or code}: {'включён' if rules['enabled'] else 'выключен'}, "
                   f"{money}, риск {float(rules['risk_pct']):g}%, стратегии "
                   f"{', '.join(rules['strategies']) or 'не выбраны'}")
    return out
