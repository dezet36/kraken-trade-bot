"""
Ядро исполнения — общие правила заявки и позиции (реорганизация, этап 5,
08.10.2026).

ЗАЧЕМ ОТДЕЛЬНО ОТ БРОКЕРА. Правила жизни сделки — когда заявка снимается, где
стоп переходит в безубыток, когда позиция выходит по сроку, сколько стоит вход и
выход — одни для бумаги и для биржи: бумага обязана вести себя как реальный
счёт по построению, а не по совпадению. Здесь только правила и арифметика, без
журналов, уведомлений, сети и денег счёта: бумажный брокер (paper_broker)
применяет их к 5-минутным свечам и сам ведёт балансы и журналы; биржевое
исполнение (этап 7) возьмёт те же правила, а налив, стоп и цели будет отдавать
биржа.

ПОРЯДОК ПРОВЕРОК — ТОТ, ЧТО ДЕРЖИТ ЗАМЕРЫ, и у каждого шага своя причина
(подробно — в комментариях paper_broker, откуда правила вынесены):
  заявка:  срок → уход за уровень по ходу сделки → налив → наблюдение →
           разрушение сетапа → цель без нас;
  позиция: пик и просадка хода → срок удержания → безубыток от уровня →
           стоп (раньше цели: порядок внутри свечи неизвестен) → цели по
           порядку (частичные, последняя закрывает).
Числа — те же операции в том же порядке, что были в брокере: эталон
tests/golden/broker_replay.json совпадает до копейки.
"""

from exit_plan import breakeven_price

FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000


def is_long(record):
    return record['direction'] == 'LONG'


# ── Заявка ──────────────────────────────────────────────────────────────────

def stop_entry(order):
    """Вход ПО ХОДУ движения (стоп-заявка): срабатывает при пробое уровня в
    сторону сделки. Иначе — лимит на откате."""
    return str(order.get('entry_type', '')).upper() in ('MARKET', 'STOP')


def limit_price(entry, is_long_, offset):
    """Цена лимита: расчётный вход, сдвинутый на offset против нас (плата за
    гарантированное касание)."""
    return entry * (1 + offset) if is_long_ else entry * (1 - offset)


def position_size(balance, risk_pct, sl_dist, max_units):
    """Объём и сумма риска от депозита и дистанции стопа, с пределом объёма."""
    risk_amount = balance * (risk_pct / 100)
    size = risk_amount / sl_dist
    if size > max_units:
        size = max_units
        risk_amount = size * sl_dist
    return size, risk_amount


def through_market_price(order, market, slippage_pct):
    """Лимит, оказавшийся за рынком при постановке: по рынку с проскальзыванием
    против нас, но не хуже самого лимита."""
    slip = market * (slippage_pct or 0.0)
    limit = order['limit_price']
    return min(limit, market + slip) if is_long(order) else max(limit, market - slip)


def expired(order, ts):
    """Свеча, открывшаяся после срока, заявку уже не застаёт."""
    return ts >= order['expires_ts']


def gone_beyond(order, high, low):
    """Цена ушла за уровень по ходу сделки до налива (params['cancel_beyond'])."""
    beyond = order.get('cancel_beyond')
    if not beyond:
        return None
    gone = (high > beyond) if is_long(order) else (low < beyond)
    return beyond if gone else None


def fill_price(order, high, low, open_price=None):
    """Цена налива на свече (симуляция) или None. Стоп-заявка при разрыве через
    уровень исполняется по открытию свечи."""
    long_ = is_long(order)
    limit = order['limit_price']
    if stop_entry(order):
        filled = (high >= limit) if long_ else (low <= limit)
    else:
        filled = (low <= limit) if long_ else (high >= limit)
    if not filled:
        return None
    price = limit
    if stop_entry(order) and open_price is not None:
        gapped = (open_price > limit) if long_ else (open_price < limit)
        if gapped:
            price = open_price
    return price


def observe(order, high, low):
    """Как близко подошла цена и сколько ушла без нас — для журнала снятых
    заявок; решений не меняет."""
    long_ = is_long(order)
    limit = order['limit_price']
    risk = abs(limit - order['stop_loss'])
    gap = ((low - limit) if long_ != stop_entry(order) else (limit - high)) / limit * 100
    run = max(0.0, ((high - limit) if long_ else (limit - low)) / risk) if risk else 0.0
    order['min_gap_pct'] = round(min(order.get('min_gap_pct', gap), gap), 4)
    order['best_run_r'] = round(max(order.get('best_run_r', run), run), 3)


def broken(order, high, low):
    """Цена дошла до уровня, за которым сетапа нет (order['invalidation'])."""
    inv = order.get('invalidation')
    if not inv:
        return None
    hit = (low <= inv) if is_long(order) else (high >= inv)
    return inv if hit else None


def target_gone(order, high, low):
    """Цена дошла до первой цели без налива."""
    target = order['targets'][0]
    return (high >= target) if is_long(order) else (low <= target)


def simulate_pending(order, ts, high, low, open_price=None, drops_at_target=True):
    """
    Одна свеча для заявки на бумаге. Возвращает (что случилось, подробность):
      ('expired', None)       срок вышел;
      ('beyond', уровень)     цена ушла за уровень по ходу сделки;
      ('fill', цена)          налив (комиссия — тейкер у стоп-заявки);
      ('broken', уровень)     сетап разрушен;
      ('target', None)        цена дошла до цели без нас (если стратегия снимает);
      (None, None)            заявка ждёт.
    """
    if expired(order, ts):
        return 'expired', None
    beyond = gone_beyond(order, high, low)
    if beyond:
        return 'beyond', beyond
    price = fill_price(order, high, low, open_price)
    if price is not None:
        return 'fill', price
    observe(order, high, low)
    inv = broken(order, high, low)
    if inv:
        return 'broken', inv
    if target_gone(order, high, low) and drops_at_target:
        return 'target', None
    return None, None


# ── Позиция ─────────────────────────────────────────────────────────────────

def entry_fee(size, price, taker, maker_fee, taker_fee):
    return size * price * (taker_fee if taker else maker_fee)


def apply_funding(position, ts, rate):
    """Фандинг за каждый пройденный 8-часовой интервал (положительная ставка —
    лонги платят шортам)."""
    if not rate:
        position['funding_ts'] = ts
        return
    last = position.get('funding_ts', position['opened_ts'])
    periods = (ts // FUNDING_INTERVAL_MS) - (last // FUNDING_INTERVAL_MS)
    if periods <= 0:
        return
    notional = position['size'] * position['last_price']
    sign = 1 if position['direction'] == 'LONG' else -1
    position['funding_paid'] = position.get('funding_paid', 0.0) + sign * rate * notional * periods
    position['funding_ts'] = ts


def track_extremes(pos, ts, high, low):
    """Пик и худшая точка хода с моментом, и когда впервые был +1R."""
    long_ = is_long(pos)
    best = max(pos['mfe_price'], high) if long_ else min(pos['mfe_price'], low)
    if best != pos['mfe_price']:
        pos['mfe_price'], pos['mfe_ts'] = best, ts
    worst = min(pos['mae_price'], low) if long_ else max(pos['mae_price'], high)
    if worst != pos['mae_price']:
        pos['mae_price'], pos['mae_ts'] = worst, ts
    if not pos.get('r1_ts'):
        dist = abs(pos['entry_price'] - pos['initial_stop'])
        if dist and ((best - pos['entry_price']) if long_ else (pos['entry_price'] - best)) >= dist:
            pos['r1_ts'] = ts


def hold_over(pos, ts, max_hold_hours):
    return bool(max_hold_hours) and (ts - pos['opened_ts']) / 3_600_000 > max_hold_hours


def breakeven_from_level(pos, high, low):
    """Стоп в безубыток при пересечении уровня be_level — если стратегия его
    заказала. True — стоп перенесён."""
    if not (pos.get('breakeven_after_tp', True) and not pos['breakeven_set']):
        return False
    be = pos.get('be_level')
    if not be:
        return False
    crossed = (high >= be) if is_long(pos) else (low <= be)
    if not crossed:
        return False
    pos['stop_loss'] = breakeven_price(pos['entry_price'], is_long(pos))
    pos['breakeven_set'] = True
    return True


def stop_hit(pos, high, low):
    stop = pos['stop_loss']
    return (low <= stop) if is_long(pos) else (high >= stop)


def take_partial(pos, index, level, maker_fee):
    """Частичная фиксация на цели index (лимит в стакане — мейкер)."""
    portion = min(pos['size'], pos['initial_size'] * pos['fractions'][index])
    sign = 1 if pos['direction'] == 'LONG' else -1
    pos['realized_pnl'] += sign * (level - pos['entry_price']) * portion
    pos['fees_paid'] += portion * level * maker_fee
    pos['size'] = max(0.0, pos['size'] - portion)
    pos['tp_hit'] = index + 1


def exit_fee_rate(reason, maker_fee, taker_fee):
    """Цель — лимит в стакане (мейкер); стоп, безубыток, срок, ручное — рынком
    (тейкер)."""
    return maker_fee if str(reason).startswith('TP') else taker_fee


def close_numbers(pos, price, reason, slip, slippage_pct, maker_fee, taker_fee):
    """Итог закрытия остатка: (цена выхода, грязный итог, комиссии, фандинг,
    чистый итог). Проскальзывание всегда против нас."""
    long_ = is_long(pos)
    exit_price = price
    if slip and slippage_pct:
        move = price * slippage_pct
        exit_price = price - move if long_ else price + move
    sign = 1 if long_ else -1
    remaining = pos['size']
    gross = pos['realized_pnl'] + sign * (exit_price - pos['entry_price']) * remaining
    fees = pos['fees_paid'] + remaining * exit_price * exit_fee_rate(reason, maker_fee, taker_fee)
    funding = pos.get('funding_paid', 0.0)
    net = gross - fees - funding
    return exit_price, gross, fees, funding, net


def simulate_position(pos, ts, high, low, close, max_hold_hours, maker_fee):
    """
    Одна свеча для позиции на бумаге — генератор событий по ходу свечи. Правила
    меняют позицию на месте (пик хода, стоп в безубыток, частичные выходы), а
    событие отдаётся сразу: обработчик (журнал, уведомление) видит позицию ровно
    в том состоянии, в каком она была в этот момент, — и когда за одну свечу
    взято несколько целей тоже.
      ('breakeven', None)            стоп перенесён в безубыток от уровня;
      ('target', (номер, уровень))   частичная цель взята;
      ('close', (цена, причина, проскальзывание))  позиция закрывается — после
                                     этого события других нет.
    """
    track_extremes(pos, ts, high, low)
    if hold_over(pos, ts, max_hold_hours):
        yield 'close', (close, 'TIME', True)
        return
    if breakeven_from_level(pos, high, low):
        yield 'breakeven', None
    if stop_hit(pos, high, low):
        yield 'close', (pos['stop_loss'], 'BE' if pos['breakeven_set'] else 'SL', True)
        return
    long_ = is_long(pos)
    while pos['tp_hit'] < len(pos['targets']):
        level = pos['targets'][pos['tp_hit']]
        reached = (high >= level) if long_ else (low <= level)
        if not reached:
            break
        index = pos['tp_hit']
        pos.setdefault('tp_min', []).append(int((ts - pos['opened_ts']) / 60000))
        if index < len(pos['targets']) - 1:
            take_partial(pos, index, level, maker_fee)
            if pos.get('breakeven_after_tp', True) and not pos['breakeven_set']:
                pos['stop_loss'] = pos['entry_price']
                pos['breakeven_set'] = True
            yield 'target', (index, level)
        else:
            yield 'close', (level, f'TP{index + 1}', False)
            return
