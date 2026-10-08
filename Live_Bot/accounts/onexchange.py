"""
Биржевые торговые счета — Bybit и BingX, сначала демо: реорганизация, этап 7,
шаг 3 (решение владельца 08.10.2026: «выбрал биржу, указал в приложении
ключи — это сохранилось на сервере и началась торговля»).

Тот же счёт, что «по инструкциям» (accounts/books.py: решение по сетапу,
деньги, правила пропа), только руку владельца заменяет биржа. Заявка входа
ставится по ключам счёта (execution/venue.py) сразу со стопом — позиция не
бывает без защиты, даже когда бот выключен. Налив, тейки и стоп исполняет
биржа, а бот ведёт то, чего биржа сама не знает, — по тем же правилам ядра
исполнения на 5-минутных свечах, что тест: снять заявку по сроку, уходу за
уровень, слому сетапа или цели без нас; стоп в безубыток (от уровня или
после первого тейка); закрыть по сроку удержания.

ИСТИНА — БИРЖА. Налив, остаток позиции, цены и комиссии выхода берутся с
биржи, свечи только поднимают правила. Капитал — баланс USDT счёта на
бирже, размер этапа — капитал в его начале.

Отличия от теста, которые остаются: объём округляется вниз до шага биржи
(риск чуть меньше), заявка меньше минимума биржи не ставится; фандинг уже в
балансе биржи и в строку сделки не попадает; налив виден с опозданием до
одного цикла бота.

РЕАЛЬНЫЕ ДЕНЬГИ — ПОСЛЕ ДЕМО. Счёт в режиме live не торгует, пока не
включён EXCHANGE_LIVE_ENABLED (решение владельца: сначала демо Bybit и VST
BingX, потом малая сумма).
"""

import copy
import hashlib

from logger import log

from accounts import books
from accounts.books import DROP_TEXT, EXIT_TEXT, TARGET, move, p, qty, usd
from execution import core

KIND = 'exchange'
FAILS_TO_ALERT = 3          # подряд неудачных обращений к бирже — сообщить владельцу

_venues = {}                # код счёта → (отпечаток ключей и режима, Venue)
_fails = {}                 # код счёта → подряд неудачных обращений


def live_enabled():
    return bool(getattr(books.cfg(), 'EXCHANGE_LIVE_ENABLED', False))


def tradable(rules):
    """Торгует ли счёт на бирже: демо — да, реальные деньги — когда разрешено."""
    return rules['mode'] == 'demo' or live_enabled()


def _accounts():
    return books.accounts(KIND)


def _venue(code, rules):
    """Клиент биржи по ключам счёта; пересоздаётся, когда сменились ключи или
    режим. None — ключей нет или режим не разрешён."""
    from accounts import live
    keys = live.keys(code)
    if not keys or not tradable(rules):
        return None
    stamp = hashlib.sha256(f"{rules['exchange']}|{rules['mode']}|{keys[0]}|{keys[1]}".encode()).hexdigest()
    cached = _venues.get(code)
    if cached and cached[0] == stamp:
        return cached[1]
    import exchange
    from execution.venue import Venue
    client = exchange.make_client(rules['exchange'], keys[0], keys[1], rules['mode'].upper())
    client.timeout = 15000          # зависшая биржа не держит цикл бота по полминуты
    venue = Venue(client)
    _venues[code] = (stamp, venue)
    return venue


def _ok(code):
    _fails.pop(code, None)


def _failed(code, rules, exc, now):
    """Сбой биржи: в журнал всегда, владельцу — на третий подряд."""
    n = _fails[code] = _fails.get(code, 0) + 1
    who = rules.get('name') or code
    log(f'⚠️ [{who}] биржа {rules["exchange"]} ({rules["mode"]}): {exc}')
    if n != FAILS_TO_ALERT:
        return
    with books.lock:
        book = books.state().get(code)
        if book is None:
            return
        books.instruct(code, rules, book, 'error', 'Биржа не отвечает или не принимает ключи',
                       [f'{n} раза подряд: {str(exc)[:300]}',
                        'Проверьте ключи и права (торговля) на странице «Счета». Стоп каждой '
                        'позиции стоит на бирже и работает без бота.'],
                       action=False, now=now)
        books.save()


def wants(strategy):
    """Ждёт ли сетапы этой стратегии хоть один биржевой счёт."""
    with books.lock:
        state = books.state()
        return any(books.open_for(strategy, rules, state.get(code)) and tradable(rules)
                   for code, rules in _accounts().items())


# ── Сетап → заявка на бирже ─────────────────────────────────────────────────

def offer(strategy, setup, now_ms=None):
    """Сетап — каждому биржевому счёту, выбравшему стратегию.
    Возвращает [(счёт, None — заявка стоит | причина отказа)]."""
    now = now_ms or books.now_ms()
    out = []
    for code, rules in sorted(_accounts().items()):
        with books.lock:
            if not books.open_for(strategy, rules, books.state().get(code)):
                continue
        try:
            venue = _venue(code, rules)
            if venue is None:
                continue
            why = _place(code, rules, venue, strategy, copy.deepcopy(setup), now)
            _ok(code)
        except Exception as exc:                   # noqa: BLE001
            why = f'биржа: {str(exc)[:200]}'
            _failed(code, rules, exc, now)
        if why:
            log(f"   📤 [{rules.get('name') or code}] {strategy} "
                f"{books.norm(setup.get('trading_pair'))}: не взят — {why}")
        out.append((code, why))
    books.flush()
    return out


def _leverage(order, balance, venue):
    """Плечо: стоп ближе ликвидации (ликвидация дальше стопа на запас), объём
    помещается в капитал. None — не помещается."""
    cfg = books.cfg()
    top = int(cfg.LEVERAGE)
    most = venue.max_leverage(order['pair'])
    if most:
        top = min(top, int(most))
    stop_pct = abs(order['limit_price'] - order['stop_loss']) / order['limit_price']
    if stop_pct > 0:
        top = min(top, max(1, int(0.8 / stop_pct)))
    notional = order['size'] * order['limit_price']
    if balance <= 0 or notional > balance * top:
        return None
    return top


def _place(code, rules, venue, strategy, setup, now):
    """Решение счёта и заявка на бирже. None — стоит, иначе причина отказа."""
    balance = venue.balance()                      # сеть — вне замка
    with books.lock:
        book = books.get(code, rules, start=balance)
        book['balance'] = balance
        order, why = books.decide(rules, book, strategy, setup, now)
        books.save()
    if why:
        return why
    pair, long_ = order['pair'], order['direction'] == 'LONG'
    market = books.through_market(strategy, setup, order)
    price = market if market is not None else order['limit_price']
    amount = venue.amount(pair, order['size'])
    least_amount, least_cost = venue.minimum(pair)
    if amount <= 0 or amount < least_amount or amount * price < least_cost:
        with books.lock:
            why = books.refuse(book, 'меньше минимума биржи',
                               f'объём {qty(order["size"])} (≈{usd(order["size"] * price)})')
            books.save()
        return why
    # Объём — вниз до шага биржи: риск чуть меньше расчётного, но не больше.
    order['size'] = amount
    order['risk_amount'] = amount * abs(order['limit_price'] - order['stop_loss'])
    leverage = _leverage(order, balance, venue)
    if leverage is None:
        with books.lock:
            why = books.refuse(book, 'плечо', f'объём {usd(amount * price)} не помещается в капитал')
            books.save()
        return why
    venue.set_leverage(pair, leverage)
    kind = 'market' if market is not None else ('stop' if core.stop_entry(order) else 'limit')
    order_id = venue.place_entry(pair, long_, amount, order['limit_price'], order['stop_loss'], kind=kind)
    order.update(order_id=order_id, venue_kind=kind, leverage=leverage)
    how = {'limit': f"Лимит {p(order['limit_price'])}",
           'stop': f"Стоп-заявка на вход {p(order['limit_price'])} (по ходу движения)",
           'market': f"По рынку ≈ {p(price)} (лимит {p(order['limit_price'])} уже за рынком)"}[kind]
    with books.lock:
        books.register(book, order, now)
        books.instruct(code, rules, book, 'order', f"Заявка на бирже · {pair} {order['direction']}",
                       [f'Стратегия: {books.name(strategy)}', how] + books.plan_lines(order, price)
                       + [f"Стоп стоит на бирже вместе с заявкой; плечо {leverage}×. "
                          f"Не нальётся до {books.when(order['expires_ts'])} — сниму."],
                       pair, strategy, action=False, now=now)
        books.save()
    return None


# ── Ведение ─────────────────────────────────────────────────────────────────

def update(market_client, now_ms=None):
    """Раз в цикл бота: сверка книг с биржей и правила по свечам. Свечи — с
    рыночного клиента бота (без ключей), заявки и позиции — со счёта."""
    now = now_ms or books.now_ms()
    accounts = _accounts()
    with books.lock:
        state = books.state()
        active = {c: r for c, r in accounts.items() if c in state}
        since = books.since_by_pair([state[c] for c in active])
    bars = {pair: books.bars(market_client, pair, start, now) for pair, start in since.items()}
    for code, rules in sorted(active.items()):
        if not tradable(rules):
            continue
        try:
            venue = _venue(code, rules)
            if venue is None:
                log(f"⚠️ [{rules.get('name') or code}]: ключей нет — книга на бирже не сверяется")
                continue
            _sync(code, rules, venue, bars, now)
            _ok(code)
        except Exception as exc:                   # noqa: BLE001
            _failed(code, rules, exc, now)
    books.flush()


def _sync(code, rules, venue, bars, now):
    balance = venue.balance()
    held = venue.positions()
    with books.lock:
        book = books.get(code, rules, start=balance)
        book['balance'] = balance
        pending = list(book['pending'].items())
    for pair, order in pending:
        _sync_order(code, rules, venue, book, pair, order, bars.get(pair) or [], now)
    with books.lock:
        positions = list(book['positions'].items())
    for pair, pos in positions:
        _sync_position(code, rules, venue, book, pair, pos, bars.get(pair) or [], held, now)
    with books.lock:
        # Сверка: позиции на бирже, которых нет в книге (открыты руками или
        # остались после сбоя), — на панель; бот их не ведёт и не трогает.
        book['foreign'] = [{'pair': k, 'size': held[k]['size'], 'long': held[k]['long']}
                           for k in sorted(set(held) - set(book['positions']))]
        books.check_rules(code, rules, book, now,
                          lambda status, why: _finish(code, rules, venue, book, status, why, now))
        books.save()


def _sync_order(code, rules, venue, book, pair, order, bars, now):
    import strategy_profile
    status = venue.order(pair, order['order_id'])
    if status is None:
        return                                     # биржа не ответила — в следующий цикл
    if status['status'] == 'closed' or status['filled'] >= order['size'] * 0.999:
        _opened(code, rules, venue, book, pair, order, status['average'] or order['limit_price'],
                status['filled'] or order['size'], now)
        return
    if status['status'] in ('canceled', 'rejected', 'expired'):
        if status['filled'] > 0:
            _opened(code, rules, venue, book, pair, order, status['average'] or order['limit_price'],
                    status['filled'], now)
        else:
            _dropped(code, rules, book, pair, order, 'биржа сняла заявку', now)
        return
    # Ждёт: правила снятия по свечам те же, что у теста (execution/core),
    # кроме налива — его даёт биржа.
    why = DROP_TEXT['expired'] if core.expired(order, now) else None
    for bar in bars:
        ts, high, low, close = bar[0], bar[2], bar[3], bar[4]
        if ts <= order['last_ts']:
            continue
        order['last_ts'], order['last_price'] = ts, close
        if why:
            continue
        if core.gone_beyond(order, high, low):
            why = DROP_TEXT['beyond']
            continue
        core.observe(order, high, low)
        if core.broken(order, high, low):
            why = DROP_TEXT['broken']
        elif core.target_gone(order, high, low) and strategy_profile.drops_at_target(order['strategy']):
            why = DROP_TEXT['target']
    if not why:
        return
    venue.cancel(pair, order['order_id'])
    again = venue.order(pair, order['order_id']) or status
    if again['filled'] > 0:                        # успела налиться часть — это позиция
        _opened(code, rules, venue, book, pair, order, again['average'] or order['limit_price'],
                again['filled'], now)
    else:
        _dropped(code, rules, book, pair, order, why, now)


def _dropped(code, rules, book, pair, order, reason, now):
    with books.lock:
        book['pending'].pop(pair, None)
        dropped = book['counts']['dropped']
        dropped[reason] = dropped.get(reason, 0) + 1
        books.instruct(code, rules, book, 'cancel', f"Заявка снята · {pair} {order['direction']}",
                       [f"Вход {p(order['limit_price'])} — {reason}."], pair, order['strategy'],
                       action=False, now=now)


def _opened(code, rules, venue, book, pair, order, price, amount, now):
    """Заявка налилась на бирже: позиция в книгу и тейки — лимитами reduce-only
    по целям (мейкер, как в тесте). Стоп уже стоит — пришёл с заявкой."""
    long_ = order['direction'] == 'LONG'
    targets, fractions = order['targets'], order['fractions']
    takes, left, problems = [], amount, []
    for k, level in enumerate(targets):
        part = left if k == len(targets) - 1 else venue.amount(pair, amount * float(fractions[k]))
        if part <= 0:
            takes.append('')
            continue
        try:
            takes.append(venue.take_profit(pair, long_, part, level))
            left -= part
        except Exception as exc:                   # noqa: BLE001
            takes.append('')
            problems.append(f'тейк {k + 1} не поставлен: {str(exc)[:150]}')
    with books.lock:
        book['pending'].pop(pair, None)
        # Уплаченное — уже в балансе биржи: в позиции его не держим.
        pos = books.position(book, order, now, price, amount)
        pos.update(order_id=order['order_id'], take_orders=takes)
        book['positions'][pair] = pos
        books.instruct(code, rules, book, 'fill', f"Налилась на бирже · {pair} {order['direction']}",
                       [f"Вход {p(price)}, объём {qty(amount)}. Стоп {p(pos['stop_loss'])} на бирже, тейк"
                        + ('и' if len(targets) > 1 else '') + ': ' + ', '.join(p(t) for t in targets)]
                       + problems, pair, order['strategy'], action=False, now=now)


def _sync_position(code, rules, venue, book, pair, pos, bars, held, now):
    import strategy_profile
    from exit_plan import tps_completed
    long_ = pos['direction'] == 'LONG'
    head = f"{pair} {pos['direction']}"
    ex = held.get(pair)
    size = ex['size'] if ex and ex['long'] == long_ else 0.0
    if size <= 0:
        _closed(code, rules, venue, book, pair, pos, now)
        return
    if size < pos['size'] * 0.999:                 # исполнились тейки
        hit = tps_completed(size, pos['initial_size'], pos['fractions'])
        with books.lock:
            for index in range(pos['tp_hit'], min(hit, len(pos['targets']) - 1)):
                books.instruct(code, rules, book, 'target', f'Тейк {index + 1} взят · {head}',
                               [f"Тейк {index + 1} из {len(pos['targets'])}: {p(pos['targets'][index])} — "
                                f"на бирже осталось {qty(size)}."], pair, pos['strategy'], action=False, now=now)
            pos['size'], pos['tp_hit'] = size, max(pos['tp_hit'], hit)
        if pos['tp_hit'] and pos.get('breakeven_after_tp', True) and not pos['breakeven_set']:
            _stop_to(code, rules, venue, book, pair, pos, pos['entry_price'], 'после тейка', now)
    max_hold = pos.get('max_hold_hours') or strategy_profile.max_hold_hours(pos['strategy'])
    for bar in bars:
        ts, high, low, close = bar[0], bar[2], bar[3], bar[4]
        if ts <= pos['last_ts']:
            continue
        pos['last_ts'], pos['last_price'] = ts, close
        core.track_extremes(pos, ts, high, low)
        if pos.get('closing'):
            continue
        if core.hold_over(pos, ts, max_hold):
            venue.close_market(pair, long_, pos['size'])
            pos['closing'] = 'TIME'
            with books.lock:
                books.instruct(code, rules, book, 'close', f'Закрыта по сроку · {head}',
                               [f'Срок удержания {float(max_hold):g} ч вышел — остаток закрыт по рынку.'],
                               pair, pos['strategy'], action=False, now=now)
            continue
        trial = dict(pos)
        if core.breakeven_from_level(trial, high, low):
            _stop_to(code, rules, venue, book, pair, pos, trial['stop_loss'],
                     f"цена прошла {p(pos['be_level'])}", now)


def _stop_to(code, rules, venue, book, pair, pos, price, why, now):
    """Стоп в безубыток на бирже; в книгу — только когда биржа приняла."""
    long_ = pos['direction'] == 'LONG'
    venue.move_stop(pair, long_, price, pos['size'])
    with books.lock:
        pos['stop_loss'], pos['breakeven_set'] = price, True
        books.instruct(code, rules, book, 'breakeven', f"Стоп в безубыток · {pair} {pos['direction']}",
                       [f'{why[0].upper() + why[1:]} — стоп на бирже перенесён на {p(price)}.'],
                       pair, pos['strategy'], action=False, now=now)


def _exit_numbers(venue, pos):
    """(средняя цена выхода, последняя цена выхода, грязный итог, комиссии,
    оценка ли это) — по сделкам биржи; нет сделок — по последней цене."""
    long_ = pos['direction'] == 'LONG'
    sign = 1 if long_ else -1
    try:
        trades = venue.fills(pos['pair'], pos['placed_ts'] - 60_000)
    except Exception as exc:                       # noqa: BLE001
        log(f"   {pos['pair']}: сделки биржи недоступны ({exc}) — итог по последней цене")
        trades = []
    opening = [t for t in trades if t['side'] == ('buy' if long_ else 'sell')]
    closing = [t for t in trades if t['side'] == ('sell' if long_ else 'buy')]
    if opening and closing:
        in_qty = sum(t['amount'] for t in opening)
        out_qty = sum(t['amount'] for t in closing)
        entry = sum(t['amount'] * t['price'] for t in opening) / in_qty
        exit_avg = sum(t['amount'] * t['price'] for t in closing) / out_qty
        gross = sign * (exit_avg - entry) * min(in_qty, out_qty)
        fees = sum(t['fee'] for t in opening) + sum(t['fee'] for t in closing)
        return exit_avg, closing[-1]['price'], gross, fees, False
    cfg = books.cfg()
    price = pos.get('last_price') or pos['entry_price']
    size = pos['initial_size']
    gross = sign * (price - pos['entry_price']) * size
    fees = size * (pos['entry_price'] + price) * cfg.PAPER_FEE_TAKER
    return price, price, gross, fees, True


def _reason(pos, last_price):
    """Чем закрылась позиция на бирже — по цене последнего выхода."""
    if pos.get('closing'):
        return pos['closing']
    near = lambda a, b: b and abs(a - b) / b < 0.003              # noqa: E731
    if near(last_price, pos['targets'][-1]):
        return f"TP{len(pos['targets'])}"
    if near(last_price, pos['stop_loss']):
        return 'BE' if pos['breakeven_set'] else 'SL'
    return 'EXCHANGE'


def _closed(code, rules, venue, book, pair, pos, now):
    """Позиции на бирже больше нет: строка журнала по сделкам биржи."""
    venue.cancel_all(pair)                         # остатки тейков и стопа
    exit_avg, last, gross, fees, estimated = _exit_numbers(venue, pos)
    after = venue.balance()
    net = gross - fees
    reason = _reason(pos, last)
    with books.lock:
        before = after - net
        book['balance'] = after
        book['positions'].pop(pair, None)
        book['cooldown'].setdefault(pos['strategy'], {})[pair] = now
        row = books.trade_row(code, book, pos, now, exit_avg, reason, gross, fees, 0.0, net, before, after,
                              venue=f"{rules['exchange']}-{rules['mode']}", estimated=estimated)
        books.append_row(row)
        what = (f"тейк {reason[2:]}" if reason.startswith('TP') else EXIT_TEXT.get(reason, reason))
        books.instruct(code, rules, book, 'exit', f"Сделка закрыта · {pair} {pos['direction']} · {what}",
                       books.result_lines(book, pos, exit_avg, net, after)
                       + (['Итог оценён по последней цене: сделки биржи недоступны.'] if estimated else []),
                       pair, pos['strategy'], action=False, now=now)


def _finish(code, rules, venue, book, status, why, now):
    """Этап окончен по правилам: закрыть позиции и снять заявки на бирже."""
    lines = [why[0].upper() + why[1:] + '.']
    for pair, pos in sorted(book['positions'].items()):
        if pos.get('closing'):
            continue
        try:
            venue.close_market(pair, pos['direction'] == 'LONG', pos['size'])
            pos['closing'] = 'RULES'
            lines.append(f"Закрыта по рынку: {pair} {pos['direction']}")
        except Exception as exc:                   # noqa: BLE001
            lines.append(f"НЕ закрыта {pair} {pos['direction']}: {str(exc)[:150]} — закройте на бирже")
    for pair, order in sorted(book['pending'].items()):
        venue.cancel(pair, order['order_id'])
        book['pending'].pop(pair, None)
        lines.append(f"Снята заявка: {pair} {order['direction']} {p(order['limit_price'])}")
    books.end_phase(book, status, why, now)
    lines.append(f"Капитал {usd(books.equity(book))} ({move(books.equity(book), book['start_balance']):+.2f}% "
                 'за этап). Новые заявки не ставятся; новый этап — кнопкой на странице «Счета».')
    if status == TARGET:
        books.instruct(code, rules, book, 'pass', 'Цель счёта достигнута', lines, action=False, now=now)
    else:
        books.instruct(code, rules, book, 'rules', 'Пределы счёта нарушены — торговля остановлена',
                       lines, action=False, now=now)


# ── Действия владельца ──────────────────────────────────────────────────────

def act(code, action, item=None, pair=None, now_ms=None):
    """
    Действие владельца на панели — на бирже. Возвращает текст ответа.
      cancel     снять заявку на бирже (pair);
      close      закрыть позицию по рынку на бирже (pair);
      panic      аварийная остановка: закрыть все позиции счёта по рынку,
                 снять его заявки и выключить счёт;
      new_phase  новый этап от текущего капитала счёта;
      ack        отметить сообщение.
    """
    rules = _accounts().get(code)
    if rules is None:
        raise ValueError(f'нет биржевого счёта «{code}»')
    now = now_ms or books.now_ms()
    with books.lock:
        book = books.state().get(code)
    if book is None:
        raise ValueError('счёт ещё не торговал')
    if action == 'ack':
        with books.lock:
            return books.ack(book, item, now)
    venue = _venue(code, rules)
    if venue is None:
        raise ValueError('нет ключей или режим счёта не разрешён')
    if action == 'cancel':
        pair = books.norm(pair)
        order = book['pending'].get(pair)
        if not order:
            raise ValueError(f'{pair}: заявки нет')
        venue.cancel(pair, order['order_id'])
        status = venue.order(pair, order['order_id']) or {'filled': 0}
        if status['filled'] > 0:
            _opened(code, rules, venue, book, pair, order, status.get('average') or order['limit_price'],
                    status['filled'], now)
            reply = f'{pair}: заявка снята, налившаяся часть — позиция'
        else:
            _dropped(code, rules, book, pair, order, 'снята владельцем', now)
            reply = f'{pair}: заявка снята на бирже'
    elif action == 'close':
        pair = books.norm(pair)
        pos = book['positions'].get(pair)
        if not pos:
            raise ValueError(f'{pair}: позиции нет')
        venue.close_market(pair, pos['direction'] == 'LONG', pos['size'])
        with books.lock:
            pos['closing'] = 'MANUAL'
        reply = f'{pair}: закрытие по рынку отправлено на биржу'
    elif action == 'panic':
        reply = _panic(code, rules, venue, book, now)
    elif action == 'new_phase':
        balance = venue.balance()
        with books.lock:
            fresh = books.new_phase(code, rules, book, now, start=balance)
        reply = f"начат этап {fresh['phase']} с капиталом {usd(balance)}"
    else:
        raise ValueError('неизвестное действие')
    with books.lock:
        books.save()
    books.flush()
    return reply


def _panic(code, rules, venue, book, now):
    """Аварийный выключатель: всё своё закрыть и снять, счёт выключить. Позиции
    вне учёта бота (открытые владельцем) не трогает."""
    from accounts import live
    lines, problems = [], []
    for pair, pos in sorted(book['positions'].items()):
        if pos.get('closing'):
            continue
        try:
            venue.close_market(pair, pos['direction'] == 'LONG', pos['size'])
            with books.lock:
                pos['closing'] = 'MANUAL'
            lines.append(f"Закрыта по рынку: {pair} {pos['direction']}")
        except Exception as exc:                   # noqa: BLE001
            problems.append(f"НЕ закрыта {pair} {pos['direction']}: {str(exc)[:150]} — закройте на бирже")
    for pair, order in sorted(book['pending'].items()):
        venue.cancel(pair, order['order_id'])
        _dropped(code, rules, book, pair, order, 'аварийная остановка', now)
        lines.append(f"Снята заявка: {pair} {order['direction']}")
    live.save({'id': code, 'enabled': False})
    with books.lock:
        books.instruct(code, rules, book, 'rules', 'Аварийная остановка счёта',
                       (lines or ['Открытых позиций и заявок не было.']) + problems
                       + ['Счёт выключен: новых заявок не будет, пока его не включат на странице «Счета».'],
                       action=bool(problems), now=now)
    return 'счёт остановлен: ' + (f'закрыто и снято {len(lines)}' if lines else 'открытого не было') + (
        f', не удалось {len(problems)}' if problems else '')


# ── Для панели и журнала запуска ────────────────────────────────────────────

def report(code, rules, now_ms=None):
    """Книга счёта; пока счёт не торговал — без денег: капитал придёт с биржи."""
    with books.lock:
        stored = books.state().get(code)
        started = stored is not None
        foreign = list((stored or {}).get('foreign') or [])
    out = books.report(code, rules, now_ms)
    out['foreign'] = foreign
    if not started:
        out.update(start=None, balance=None, equity=None, return_pct=None, day_pnl=None,
                   floor=None, target_level=None, daily_limit=None, room=None)
    out.update(tradable=tradable(rules), live_enabled=live_enabled())
    return out


def describe():
    out = []
    for code, rules in sorted(_accounts().items()):
        from accounts import live
        state = ('торгует' if rules['enabled'] and tradable(rules)
                 else 'реальные деньги — после проверки на демо' if rules['enabled'] else 'выключен')
        out.append(f"   📤 {rules.get('name') or code}: {rules['exchange']} {rules['mode']}, {state}, "
                   f"ключи {'есть' if live.has_keys(code) else 'нет'}, риск {float(rules['risk_pct']):g}%, "
                   f"стратегии {', '.join(rules['strategies']) or 'не выбраны'}")
    return out
