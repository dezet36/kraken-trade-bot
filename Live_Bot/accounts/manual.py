"""
Счета «по инструкциям» — проп без API (HashHedge и подобные): реорганизация,
этап 7, шаг 2 (решения владельца 08.10.2026).

Такой счёт владелец ведёт руками, а бот решает за него то же, что за тестовый
счёт стратегии, — по правилам ЭТОГО счёта (решение — accounts/books.decide):
какие сетапы брать, на каких деньгах и не нарушит ли сделка правила пропа.
Взятый сетап становится инструкцией: поставить заявку — пара, сторона, вход,
стоп, тейки, объём.

Дальше счёт ведётся по тем же правилам, что тест: ядро исполнения
(execution/core.py) и величины стратегии (strategy_profile — срок заявки,
кулдаун, предел издержек, смещение лимита, срок удержания) на 5-минутных
свечах. Событие, где нужна рука (снять заявку, перенести стоп, закрыть по
рынку, проверить стоп после налива), — инструкция с отметкой «Готово» на
панели или кнопкой в Telegram; остальное (тейк взят, сделка закрыта стопом) —
сводка.

Книга счёта — запись ПЛАНА: что было бы на счёте, если инструкции исполнены
вовремя. Сделал иначе — снять заявку или закрыть позицию в записи кнопкой на
панели.

Цель пропа по капиталу — закрыть всё, этап пройден; предел пробит (разрыв
цены, проскальзывание) — счёт провален. Новые заявки после этого не ставятся
до «нового этапа» (ап-скейл: размер — из правил счёта).

ОТКУДА СЕТАПЫ. Из того же прохода стратегии, что идёт на тест (bot.py), —
копия без денег, до решения тестового счёта. Отдельного прогона сканеров ради
торговых счетов нет: сканеры SMCS и Фибо 12ч помечают обработанный бар, и
лишний прогон отнял бы сетап у теста. Поэтому стратегия торгуется здесь, пока
её сканирует тест (включена, есть место).
"""

import copy

from logger import log

from accounts import books
from accounts.books import BAR_MS, DROP_TEXT, EXIT_TEXT, STATUS_TEXT, TARGET, move, p, usd
from execution import core

KIND = 'manual'


def _accounts():
    return books.accounts(KIND)


def wants(strategy):
    """Ждёт ли сетапы этой стратегии хоть один счёт по инструкциям."""
    with books.lock:
        state = books.state()
        return any(books.open_for(strategy, rules, state.get(code))
                   for code, rules in _accounts().items())


# ── Сетап → инструкция ──────────────────────────────────────────────────────

def offer(strategy, setup, now_ms=None):
    """
    Сетап — каждому включённому счёту по инструкциям, выбравшему стратегию.
    Возвращает [(счёт, None — взят | причина отказа)].
    """
    now = now_ms or books.now_ms()
    out = []
    for code, rules in sorted(_accounts().items()):
        with books.lock:
            if not books.open_for(strategy, rules, books.state().get(code)):
                continue
            book = books.get(code, rules)
            why = _place(code, rules, book, strategy, copy.deepcopy(setup), now)
            books.save()
        if why:
            log(f"   📋 [{rules.get('name') or code}] {strategy} "
                f"{books.norm(setup.get('trading_pair'))}: не взят — {why}")
        out.append((code, why))
    books.flush()
    return out


def _place(code, rules, book, strategy, setup, now):
    """Решение счёта и инструкция. None — заявка поставлена, иначе причина."""
    cfg = books.cfg()
    order, why = books.decide(rules, book, strategy, setup, now)
    if why:
        return why
    books.register(book, order, now)
    pair, limit = order['pair'], order['limit_price']
    head = f"{pair} {order['direction']}"
    market = books.through_market(strategy, setup, order)
    if market is not None:
        price = core.through_market_price(order, market, cfg.PAPER_SLIPPAGE_PCT)
        books.instruct(code, rules, book, 'market', f'Войти по рынку · {head}',
                       [f'Стратегия: {books.name(strategy)}',
                        f'Цена ≈ {p(price)} — лимит {p(limit)} уже за рынком']
                       + books.plan_lines(order, price), pair, strategy, now=now)
        _fill(code, rules, book, pair, order, now, price, taker=True, quiet=True)
        return None
    kind = 'Стоп-заявка на вход (по ходу движения)' if core.stop_entry(order) else 'Лимит на вход'
    books.instruct(code, rules, book, 'place', f'Поставить заявку · {head}',
                   [f'Стратегия: {books.name(strategy)}', f'{kind}: {p(limit)}']
                   + books.plan_lines(order)
                   + [f"Не налилась до {books.when(order['expires_ts'])} — снять (напомню)."],
                   pair, strategy, now=now)
    return None


# ── Ведение по свечам ───────────────────────────────────────────────────────

def _fill(code, rules, book, pair, order, ts, price, taker=False, quiet=False):
    cfg = books.cfg()
    book['pending'].pop(pair, None)
    fee = core.entry_fee(order['size'], price, taker, cfg.PAPER_FEE_MAKER, cfg.PAPER_FEE_TAKER)
    pos = books.position(book, order, ts, price, order['size'], fees_paid=fee)
    book['positions'][pair] = pos
    if not quiet:
        books.instruct(code, rules, book, 'fill', f"Заявка налилась · {pair} {pos['direction']}",
                       [f'Вход {p(price)}. Проверьте, что стоят стоп {p(pos["stop_loss"])} и тейк'
                        + ('и' if len(pos['targets']) > 1 else '') + ': '
                        + ', '.join(p(t) for t in pos['targets'])],
                       pair, pos['strategy'], now=ts)


def _drop(code, rules, book, pair, reason, ts, quiet=False):
    order = book['pending'].pop(pair, None)
    if not order:
        return
    dropped = book['counts']['dropped']
    dropped[reason] = dropped.get(reason, 0) + 1
    if not quiet:
        books.instruct(code, rules, book, 'cancel', f"Снять заявку · {pair} {order['direction']}",
                       [f"Вход {p(order['limit_price'])} — {reason}."], pair, order['strategy'], now=ts)


def _close(code, rules, book, pair, pos, ts, price, reason, slip, quiet=False):
    cfg = books.cfg()
    exit_price, gross, fees, funding, net = core.close_numbers(
        pos, price, reason, slip, cfg.PAPER_SLIPPAGE_PCT, cfg.PAPER_FEE_MAKER, cfg.PAPER_FEE_TAKER)
    before = book['balance']
    after = before + net
    book['balance'] = after
    book['positions'].pop(pair, None)
    book['cooldown'].setdefault(pos['strategy'], {})[pair] = ts
    row = books.trade_row(code, book, pos, ts, exit_price, reason, gross, fees, funding, net, before, after)
    books.append_row(row)
    if quiet:
        return row
    head = f"{pair} {pos['direction']}"
    result = books.result_lines(book, pos, exit_price, net, after)
    if reason == 'TIME':
        from strategies import strategy_profile
        hold = pos.get('max_hold_hours') or strategy_profile.max_hold_hours(pos['strategy'])
        books.instruct(code, rules, book, 'close', f'Закрыть по рынку · {head}',
                       [f"Срок удержания{' ' + format(float(hold), 'g') + ' ч' if hold else ''} вышел — "
                        f"закрыть остаток по рынку."] + result, pair, pos['strategy'], now=ts)
    else:
        what = (f"тейк {reason[2:]}" if reason.startswith('TP') else EXIT_TEXT.get(reason, reason))
        books.instruct(code, rules, book, 'exit', f'Сделка закрыта · {head} · {what}', result,
                       pair, pos['strategy'], action=False, now=ts)
    return row


def _step(code, rules, book, pair, bar, funding_rate):
    """Одна закрытая 5-минутная свеча для заявки и позиции счёта по паре."""
    from strategies import strategy_profile
    cfg = books.cfg()
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
            books.instruct(code, rules, book, 'breakeven', f'Стоп в безубыток · {head}',
                           [f"Цена прошла {p(pos['be_level'])} — перенесите стоп на {p(pos['stop_loss'])}."],
                           pair, pos['strategy'], now=ts)
        elif what == 'target':
            index, level = detail
            lines = [f"Тейк {index + 1} из {len(pos['targets'])}: {p(level)} — "
                     f"закрыто {float(pos['fractions'][index]) * 100:.0f}%."]
            moved = pos['breakeven_set'] and not was_be
            if moved:
                lines.append(f"Перенесите стоп остатка в безубыток: {p(pos['stop_loss'])}.")
            books.instruct(code, rules, book, 'target', f'Тейк {index + 1} взят · {head}', lines,
                           pair, pos['strategy'], action=moved, now=ts)
        elif what == 'close':
            price, reason, slip = detail
            _close(code, rules, book, pair, pos, ts, price, reason, slip)
        was_be = pos.get('breakeven_set', was_be)


def update(client, now_ms=None):
    """Раз в цикл бота: свечи по парам счетов, события заявок и позиций,
    правила пропа. Ведётся и выключенный счёт: выключение запрещает новые
    заявки, а не ведение открытых."""
    now = now_ms or books.now_ms()
    accounts = _accounts()
    with books.lock:
        state = books.state()
        since = books.since_by_pair([state[c] for c in accounts if c in state])
    # Сеть — вне замка: панель и Telegram не ждут свечей.
    bars = {pair: books.bars(client, pair, start, now) for pair, start in since.items()}
    rates = {pair: books.funding_rate(client, pair, now) for pair in since}
    with books.lock:
        state = books.state()
        touched = False
        for code, rules in sorted(accounts.items()):
            book = state.get(code)
            if not book:
                continue
            touched = True
            for pair in sorted(set(book['pending']) | set(book['positions'])):
                for bar in bars.get(pair) or []:
                    _step(code, rules, book, pair, bar, rates.get(pair, 0.0))
            books.check_rules(code, rules, book, now,
                              lambda status, why, c=code, r=rules, b=book: _finish(c, r, b, status, why, now))
        if touched:
            books.save()
    books.flush()


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
        lines.append(f"Снять заявку: {pair} {order['direction']} {p(order['limit_price'])}")
        _drop(code, rules, book, pair, 'правила пропа', now, quiet=True)
    acted = len(lines) > 1
    books.end_phase(book, status, why, now)
    lines.append(f"Счёт {usd(book['balance'])} ({move(book['balance'], book['start_balance']):+.2f}% за этап). "
                 'Новые заявки не ставятся; новый этап — кнопкой на странице «Счета».')
    if status == TARGET:
        books.instruct(code, rules, book, 'pass', 'Цель пропа достигнута', lines, action=acted, now=now)
    else:
        books.instruct(code, rules, book, 'rules', 'Правила пропа нарушены — торговля остановлена',
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
    now = now_ms or books.now_ms()
    with books.lock:
        book = books.get(code, rules, create=(action == 'new_phase'))
        if book is None:
            raise ValueError('по счёту ещё не было заявок')
        if action == 'ack':
            return books.ack(book, item, now)
        if action == 'cancel':
            pair = books.norm(pair)
            if pair not in book['pending']:
                raise ValueError(f'{pair}: заявки нет')
            _drop(code, rules, book, pair, 'снята владельцем', now, quiet=True)
            books.save()
            return f'{pair}: заявка убрана из записи счёта'
        if action == 'close':
            pair = books.norm(pair)
            pos = book['positions'].get(pair)
            if not pos:
                raise ValueError(f'{pair}: позиции нет')
            price = pos.get('last_price') or pos['entry_price']
            _close(code, rules, book, pair, pos, now, price, 'MANUAL', slip=True, quiet=True)
            books.save()
            return f'{pair}: позиция закрыта в записи по {p(price)}'
        if action == 'new_phase':
            fresh = books.new_phase(code, rules, book, now)
            reply = f"начат этап {fresh['phase']}"
        else:
            raise ValueError('неизвестное действие')
    books.flush()
    return reply


# ── Для панели и журнала запуска ────────────────────────────────────────────

def report(code, rules, now_ms=None):
    return books.report(code, rules, now_ms)


def describe():
    """Строки для журнала запуска: какие счета по инструкциям ведутся."""
    out = []
    for code, rules in sorted(_accounts().items()):
        with books.lock:
            book = books.state().get(code)
        money = (f"этап {book['phase']}, счёт {usd(book['balance'])} — {STATUS_TEXT[book['status']]}"
                 if book else f"счёт {usd(rules.get('deposit') or 0)}, заявок ещё не было")
        out.append(f"   📋 {rules.get('name') or code}: {'включён' if rules['enabled'] else 'выключен'}, "
                   f"{money}, риск {float(rules['risk_pct']):g}%, стратегии "
                   f"{', '.join(rules['strategies']) or 'не выбраны'}")
    return out
