"""
Экраны Telegram-панели: текст и кнопки. Только рисование — ни сети, ни
действий: данные собирает и действия выполняет telegram_bot.

КАК ЧИТАЕТСЯ ПАНЕЛЬ. Одно сообщение-меню, которое перерисовывается на месте:
главная → позиции → карточка позиции → подтверждение → итог. Кнопка «назад»
есть на каждом экране, «обновить» — на каждом, где числа живые.

ДАННЫЕ ПАНЕЛИ (словарь d):
    mode, now, paused, muted, cycle_min, model
    strategies  {имя: start_balance, balance, equity, return_pct, pending, reset_at}
    open        [позиции в виде PaperBroker._position_view]
    pending     [заявки в виде PaperBroker._pending_view]
    settings    настройки оператора (settings_store.load())
    trades      закрытые сделки в виде dashboard._read_paper_trades()
    ai          strategy_llm.current_setups()

КОД КНОПКИ — не длиннее 64 байт (предел Telegram): «p:LLM:DOGEUSDT».
"""

from datetime import datetime, timedelta, timezone

import tg_format as fmt

PAGE = 10                      # позиций/заявок на страницу списка

# События уведомлений: (ключ настройки, подпись на кнопке, что это).
EVENTS = (
    ('trade_opened', 'Входы', 'вход в сделку: цена, стоп, цели, риск, почему'),
    ('trade_closed', 'Закрытия', 'выход: итог в $ и R, причина, сколько держали'),
    ('tp_hit', 'Цели', 'взята частичная цель, стоп перенесён'),
    ('breakeven', 'Безубыток', 'стоп перенесён в безубыток'),
    ('plan_dropped', 'Заявки сняты', 'заявка или план умерли, не став сделкой'),
    ('llm_setup', 'Планы ИИ', 'план модели с графиком'),
    ('llm_rejected', 'Отказы ИИ', 'план модели отклонён проверками'),
    ('error', 'Ошибки', 'простой бота, обрыв потоков, термостат'),
    ('daily', 'Итог дня', 'сводка по стратегиям после полуночи UTC'),
    ('service', 'Запуск бота', 'бот перезапущен'),
)

PERIODS = (('d', 'Сегодня'), ('7', '7 дней'), ('30', '30 дней'), ('all', 'Всё время'))


# ── Кнопки ───────────────────────────────────────────────────────────────────

def keyboard(rows):
    """[[(подпись, код), ...], ...] → inline_keyboard. Пустые ряды пропускаются."""
    out = []
    for row in rows:
        row = [b for b in row if b]
        if row:
            out.append([{'text': text, 'callback_data': data} for text, data in row])
    return {'inline_keyboard': out}


def _nav(back=None, refresh=None):
    row = []
    if back:
        row.append(back)
    row.append(('🏠 Меню', 'm'))
    if refresh:
        row.append(('🔄', refresh))
    return row


# ── Выборки ──────────────────────────────────────────────────────────────────

def _now(d):
    return d.get('now') or datetime.now(timezone.utc)


def _strategies(d):
    return fmt.ordered((d.get('strategies') or {}).keys())


def _settings_of(d, name):
    return (d.get('settings') or {}).get(name) or {}


def _closed(d, strategy=None, since=None):
    """Закрытые сделки, по которым считается статистика стратегии: после её перезапуска."""
    out = []
    for t in d.get('trades') or []:
        name = t.get('strategy')
        if strategy and name != strategy:
            continue
        closed = fmt.parse_time(t.get('closed'))
        if closed is None:
            continue
        started = fmt.parse_time(((d.get('strategies') or {}).get(name) or {}).get('reset_at'))
        if started and closed < started:
            continue
        if since and closed < since:
            continue
        out.append(t)
    return out


def _today_start(d):
    now = _now(d)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _since(d, period):
    now = _now(d)
    if period == 'd':
        return _today_start(d)
    if period in ('7', '30'):
        return now - timedelta(days=int(period))
    return None


def summarise(trades):
    """
    Сводка по сделкам — ТЕМ ЖЕ кодом, что на сайте (dashboard._summarise):
    сделки с измеренной дырой в свечах не в счёт, и число в Telegram обязано
    совпасть с числом на панели сайта.
    """
    try:
        from dashboard import _summarise
        return _summarise(list(trades))
    except Exception:                                  # noqa: BLE001
        pnl = [fmt.num(t.get('pnl')) for t in trades]
        wins = [p for p in pnl if p > 0]
        loss = abs(sum(p for p in pnl if p <= 0))
        return {'trades': len(pnl), 'wins': len(wins),
                'winrate': round(len(wins) / len(pnl) * 100, 1) if pnl else 0.0,
                'pnl': round(sum(pnl), 2),
                'profit_factor': round(sum(wins) / loss, 3) if loss else None,
                'sum_r': round(sum(fmt.num(t.get('pnl_r')) for t in trades), 2),
                'skipped': 0, 'trades_all': len(pnl)}


def _find(items, strategy, pair):
    for item in items or []:
        if item.get('strategy') == strategy and item.get('pair') == pair:
            return item
    return None


def _grouped(items):
    """Позиции/заявки по стратегиям в порядке показа, внутри — по времени."""
    by = {}
    for item in items or []:
        by.setdefault(item.get('strategy'), []).append(item)
    out = []
    for name in fmt.ordered(by):
        out.append((name, sorted(by[name], key=lambda i: str(i.get('opened') or ''))))
    return out


def _page(items, page):
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = min(max(0, int(page or 0)), pages - 1)
    return items[page * PAGE:(page + 1) * PAGE], page, pages


def _pager(kind, page, pages):
    if pages <= 1:
        return []
    return [('◀', f'{kind}:{page - 1}') if page > 0 else None,
            (f'{page + 1}/{pages}', f'{kind}:{page}'),
            ('▶', f'{kind}:{page + 1}') if page < pages - 1 else None]


# ── Главная ──────────────────────────────────────────────────────────────────

def _health(d):
    if d.get('paused'):
        state = '⏸ <b>пауза</b>: новые входы стоят, позиции ведутся'
    else:
        state = '▶️ торгует'
    cycle = d.get('cycle_min')
    if cycle is not None:
        if cycle > 12:
            state += f' · ⚠️ цикл {fmt.duration(cycle)} назад'
        else:
            state += f' · цикл {int(cycle)} мин назад'
    if d.get('model'):
        state += f" · ИИ разбирает {fmt.esc(fmt.coin(d['model']))}"
    return state


def main_view(d):
    strategies = d.get('strategies') or {}
    names = _strategies(d)
    equity = sum(fmt.num(strategies[n].get('equity')) for n in names)
    start = sum(fmt.num(strategies[n].get('start_balance')) for n in names)
    since_start = (equity / start - 1) * 100 if start else 0.0
    today = _closed(d, since=_today_start(d))
    today_pnl = sum(fmt.num(t.get('pnl')) for t in today)
    wins = sum(1 for t in today if fmt.num(t.get('pnl')) > 0)
    open_ = d.get('open') or []
    pending = d.get('pending') or []
    floating = sum(fmt.num(p.get('unrealised')) for p in open_)
    ai = d.get('ai') or {}
    armed = len(ai.get('armed') or [])

    lines = [
        f"<b>Kraken</b> · {fmt.mode_label(d.get('mode'))}",
        _health(d),
        fmt.RULE,
        f"💼 Капитал <b>{fmt.money(equity, signed=False)}</b> · с начала {fmt.pct(since_start)}",
        f"📅 Сегодня <b>{fmt.money(today_pnl)}</b> · "
        + (f"{len(today)} сд ({wins}✅ {len(today) - wins}❌)" if today else 'сделок не было'),
        f"📈 Позиций {len(open_)}" + (f" · плавает <b>{fmt.money(floating)}</b>" if open_ else ''),
        f"⏳ Заявок {len(pending)} · 🧠 планов ИИ {armed}",
    ]
    if names:
        lines += ['', '<b>Стратегии</b> · капитал · с начала · сегодня']
        for n in names:
            s = strategies[n]
            on = _settings_of(d, n).get('enabled', True)
            day = sum(fmt.num(t.get('pnl')) for t in today if t.get('strategy') == n)
            lines.append(f"{'🟢' if on else '⏸'} {fmt.name(n)} · "
                         f"{fmt.money(s.get('equity'), signed=False)} · "
                         f"{fmt.pct(s.get('return_pct'))} · {fmt.money(day)}")
    lines.append(f"<i>обновлено {_now(d).strftime('%H:%M')} UTC</i>")

    kb = keyboard([
        [(f'📈 Позиции · {len(open_)}', 'pl:0'), (f'⏳ Заявки · {len(pending)}', 'ol:0')],
        [(f'🧠 ИИ · {armed}', 'ai'), ('📊 Статистика', 'st:7')],
        [('🎛 Стратегии', 'sl'), ('🔔 Уведомления', 'nt')],
        [('▶️ Снять паузу' if d.get('paused') else '⏸ Пауза входов', 'pz'), ('🔄 Обновить', 'm')],
    ])
    return '\n'.join(lines), kb


# ── Позиции ──────────────────────────────────────────────────────────────────

def _position_line(p, now=None):
    return (f"{fmt.side_icon(p.get('direction'))} {fmt.esc(fmt.coin(p.get('pair')))} "
            f"{p.get('direction', '')} · <b>{fmt.r(p.get('unrealised_r'))}</b> "
            f"{fmt.money(p.get('unrealised'))} · {fmt.duration(fmt.minutes_since(p.get('opened'), now) or 0)}")


def positions_view(d, page=0):
    open_ = d.get('open') or []
    if not open_:
        return ('📭 <b>Открытых позиций нет</b>',
                keyboard([[('⏳ Заявки', 'ol:0'), ('🧠 ИИ', 'ai')], _nav(refresh='pl:0')]))
    flat = [p for _name, items in _grouped(open_) for p in items]
    shown, page, pages = _page(flat, page)
    floating = sum(fmt.num(p.get('unrealised')) for p in open_)
    floating_r = sum(fmt.num(p.get('unrealised_r')) for p in open_)
    lines = [f"📈 <b>Позиции · {len(open_)}</b> · плавает {fmt.money(floating)} · {fmt.r(floating_r)}",
             fmt.RULE]
    current = None
    for p in shown:
        if p.get('strategy') != current:
            current = p.get('strategy')
            group = [x for x in open_ if x.get('strategy') == current]
            lines.append(f"<b>{fmt.name(current)}</b> · {len(group)} · "
                         f"{fmt.money(sum(fmt.num(x.get('unrealised')) for x in group))}")
        lines.append(_position_line(p, _now(d)))
    lines.append('<i>нажмите позицию — карточка и управление</i>')
    buttons = [(f"{fmt.side_icon(p.get('direction'))} {fmt.coin(p.get('pair'))} · {fmt.name(p.get('strategy'))}",
                f"p:{p.get('strategy')}:{p.get('pair')}") for p in shown]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append(_pager('pl', page, pages))
    rows.append(_nav(refresh=f'pl:{page}'))
    return '\n'.join(lines), keyboard(rows)


def _can_breakeven(p):
    entry, stop = fmt.num(p.get('entry')), fmt.num(p.get('stop'))
    return (stop < entry) if p.get('direction') == 'LONG' else (stop > entry)


def position_view(d, strategy, pair):
    p = _find(d.get('open'), strategy, pair)
    if p is None:
        return (f"⚪ <b>{fmt.esc(pair)}</b> · {fmt.name(strategy)}: позиции уже нет — закрыта.",
                keyboard([[('◀ Позиции', 'pl:0')], _nav()]))
    entry, stop, price = fmt.num(p.get('entry')), fmt.num(p.get('stop')), fmt.num(p.get('price'))
    initial = fmt.num(p.get('initial_stop')) or stop
    risk_dist = abs(entry - initial) or 1e-12
    sign = 1 if p.get('direction') == 'LONG' else -1
    targets = p.get('targets') or []
    fractions = p.get('fractions') or []
    lines = [
        f"{fmt.side_icon(p.get('direction'))} <b>{fmt.esc(pair)} · {p.get('direction')}</b> · {fmt.name(strategy)}",
        fmt.RULE,
        f"Сейчас <b>{fmt.price(price)}</b> · <b>{fmt.r(p.get('unrealised_r'))}</b> ({fmt.money(p.get('unrealised'))})",
        f"Вход {fmt.price(entry)} · {fmt.clock(p.get('opened'))} · "
        f"{fmt.duration(fmt.minutes_since(p.get('opened'), _now(d)) or 0)}",
        f"Стоп {fmt.price(stop)} · {fmt.pct((stop - entry) / entry * 100 if entry else 0)} от входа"
        + (f" · до стопа {abs(price - stop) / price * 100:.2f}%" if price else ''),
    ]
    taken = int(p.get('tp_hit') or 0)
    for k, t in enumerate(targets):
        mark = '✅' if k < taken else '🎯'
        share = f" · {fmt.num(fractions[k]) * 100:.0f}%" if k < len(fractions) else ''
        lines.append(f"{mark} Цель {k + 1} {fmt.price(t)}{share} · "
                     f"{fmt.r(sign * (fmt.num(t) - entry) / risk_dist)}")
    lines.append(f"Целей взято {taken}/{len(targets)} · безубыток {'да' if p.get('breakeven') else 'нет'}"
                 + (f" · осталось {fmt.num(p.get('size_left_pct')):.0f}%" if taken else ''))
    lines.append(f"Лучший ход {fmt.r(p.get('mfe_r'))} · зафиксировано {fmt.money(p.get('realized'))}")
    lines.append(f"Позиция {fmt.money(fmt.num(p.get('size')) * price, signed=False)} · "
                 f"риск {fmt.money(p.get('risk'), signed=False)} · издержки {fmt.money(p.get('costs'), signed=False)}")
    if p.get('why'):
        lines += [fmt.RULE, f"<i>{fmt.esc(fmt.cut(p['why'], 300))}</i>"]
    if p.get('price_at'):
        lines.append(f"<i>цена на {fmt.clock(p['price_at'])[6:]}</i>")
    code = f'{strategy}:{pair}'
    kb = keyboard([
        [('🛡 Стоп в безубыток', f'pb:{code}') if _can_breakeven(p) else None, ('❌ Закрыть', f'pc:{code}')],
        [('◀ Позиции', 'pl:0'), ('🏠 Меню', 'm'), ('🔄', f'p:{code}')],
    ])
    return '\n'.join(lines), kb


def close_confirm_view(d, strategy, pair, token):
    p = _find(d.get('open'), strategy, pair)
    if p is None:
        return position_view(d, strategy, pair)
    text = '\n'.join([
        f"❌ <b>Закрыть {fmt.esc(pair)} {p.get('direction')} · {fmt.name(strategy)}?</b>",
        fmt.RULE,
        f"По рынку ≈ {fmt.price(p.get('price'))} (последняя цена)",
        f"Итог ≈ <b>{fmt.r(p.get('unrealised_r'))}</b> ({fmt.money(p.get('unrealised'))}), "
        f"плюс проскальзывание и комиссия выхода",
        'Отменить будет нельзя.',
    ])
    return text, keyboard([[('✅ Да, закрыть', f'y:c:{token}:{strategy}:{pair}'),
                            ('↩️ Нет', f'p:{strategy}:{pair}')]])


def breakeven_confirm_view(d, strategy, pair, token):
    p = _find(d.get('open'), strategy, pair)
    if p is None or not _can_breakeven(p):
        return position_view(d, strategy, pair)
    text = '\n'.join([
        f"🛡 <b>Стоп в безубыток · {fmt.esc(pair)} {p.get('direction')} · {fmt.name(strategy)}</b>",
        fmt.RULE,
        f"Стоп {fmt.price(p.get('stop'))} → {fmt.price(p.get('entry'))} (вход).",
        'Риск по сделке станет нулевым; отодвинуть стоп обратно нельзя.',
    ])
    return text, keyboard([[('✅ Перенести', f'y:b:{token}:{strategy}:{pair}'),
                            ('↩️ Нет', f'p:{strategy}:{pair}')]])


# ── Заявки ───────────────────────────────────────────────────────────────────

def _order_line(o):
    dist = o.get('distance_pct')
    return (f"{fmt.side_icon(o.get('direction'))} {fmt.esc(fmt.coin(o.get('pair')))} "
            f"{o.get('direction', '')} от {fmt.price(o.get('entry'))}"
            + (f" · до цены {fmt.num(dist):.2f}%" if dist is not None else '')
            + f" · ещё {fmt.duration(o.get('expires_in_min'))}")


def orders_view(d, page=0):
    pending = d.get('pending') or []
    if not pending:
        return ('📭 <b>Заявок нет</b> — лимиты не ждут цену.',
                keyboard([[('📈 Позиции', 'pl:0'), ('🧠 ИИ', 'ai')], _nav(refresh='ol:0')]))
    flat = [o for _name, items in _grouped(pending) for o in items]
    shown, page, pages = _page(flat, page)
    lines = [f"⏳ <b>Заявки · {len(pending)}</b> · лимиты ждут цену", fmt.RULE]
    current = None
    for o in shown:
        if o.get('strategy') != current:
            current = o.get('strategy')
            lines.append(f"<b>{fmt.name(current)}</b> · "
                         f"{sum(1 for x in pending if x.get('strategy') == current)}")
        lines.append(_order_line(o))
    buttons = [(f"{fmt.side_icon(o.get('direction'))} {fmt.coin(o.get('pair'))} · {fmt.name(o.get('strategy'))}",
                f"o:{o.get('strategy')}:{o.get('pair')}") for o in shown]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append(_pager('ol', page, pages))
    rows.append(_nav(refresh=f'ol:{page}'))
    return '\n'.join(lines), keyboard(rows)


def order_view(d, strategy, pair):
    o = _find(d.get('pending'), strategy, pair)
    if o is None:
        return (f"⚪ <b>{fmt.esc(pair)}</b> · {fmt.name(strategy)}: заявки уже нет — налилась или снята.",
                keyboard([[('◀ Заявки', 'ol:0'), ('📈 Позиции', 'pl:0')], _nav()]))
    entry, stop = fmt.num(o.get('entry')), fmt.num(o.get('stop'))
    lines = [
        f"{fmt.side_icon(o.get('direction'))} <b>{fmt.esc(pair)} · {o.get('direction')}</b> · "
        f"{fmt.name(strategy)} · заявка",
        fmt.RULE,
        f"Лимит {fmt.price(entry)}"
        + (f" · цена {fmt.price(o.get('price'))} (до лимита {fmt.num(o.get('distance_pct')):.2f}%)"
           if o.get('price') else ''),
        f"Стоп {fmt.price(stop)} · {fmt.pct((stop - entry) / entry * 100 if entry else 0)}",
    ]
    for k, t in enumerate(o.get('targets') or []):
        lines.append(f"🎯 Цель {k + 1} {fmt.price(t)}")
    lines.append(f"R:R {fmt.num(o.get('rr')):.2f} · риск {fmt.money(o.get('risk'), signed=False)}")
    lines.append(f"Ждёт {fmt.duration(o.get('waiting_min'))} · снимется через {fmt.duration(o.get('expires_in_min'))}")
    if o.get('why'):
        lines += [fmt.RULE, f"<i>{fmt.esc(fmt.cut(o['why'], 300))}</i>"]
    code = f'{strategy}:{pair}'
    return '\n'.join(lines), keyboard([
        [('🗑 Снять заявку', f'oc:{code}')],
        [('◀ Заявки', 'ol:0'), ('🏠 Меню', 'm'), ('🔄', f'o:{code}')],
    ])


def cancel_confirm_view(d, strategy, pair, token):
    o = _find(d.get('pending'), strategy, pair)
    if o is None:
        return order_view(d, strategy, pair)
    text = '\n'.join([
        f"🗑 <b>Снять заявку {fmt.esc(pair)} {o.get('direction')} · {fmt.name(strategy)}?</b>",
        fmt.RULE,
        f"Лимит {fmt.price(o.get('entry'))}. В журнале сетапов она будет записана как «снята оператором».",
    ])
    return text, keyboard([[('✅ Снять', f'y:x:{token}:{strategy}:{pair}'),
                            ('↩️ Нет', f'o:{strategy}:{pair}')]])


# ── ИИ ───────────────────────────────────────────────────────────────────────

def ai_view(d):
    import telegram_notify as tg
    text = tg.llm_setups_text(d.get('ai') or {'armed': [], 'pending': [], 'open': []})
    if len(text) > 3900:
        text = text[:3890].rsplit('\n', 1)[0] + '\n…'
    return text, keyboard([[('📈 Позиции', 'pl:0'), ('⏳ Заявки', 'ol:0')], _nav(refresh='ai')])


# ── Статистика ───────────────────────────────────────────────────────────────

def _stat_line(label, s):
    if not s.get('trades'):
        return f"{label} · сделок нет"
    pf = s.get('profit_factor')
    return (f"{label} · {s['trades']} сд · {s.get('winrate', 0):.0f}% · "
            f"PF {('∞' if pf is None and s.get('wins') else (f'{pf:.2f}' if pf is not None else '—'))} · "
            f"<b>{fmt.r(s.get('sum_r'))}</b> · {fmt.money(s.get('pnl'))}")


def stats_view(d, period='7'):
    period = period if period in dict(PERIODS) else '7'
    since = _since(d, period)
    trades = _closed(d, since=since)
    lines = [f"📊 <b>Статистика · {dict(PERIODS)[period].lower()}</b>", fmt.RULE]
    total = summarise(trades)
    lines.append(_stat_line('<b>Всего</b>', total))
    for n in _strategies(d):
        lines.append(_stat_line(fmt.name(n), summarise([t for t in trades if t.get('strategy') == n])))
    if trades:
        best = max(trades, key=lambda t: fmt.num(t.get('pnl_r')))
        worst = min(trades, key=lambda t: fmt.num(t.get('pnl_r')))
        lines.append(fmt.RULE)
        lines.append(f"🏆 {fmt.esc(fmt.coin(best.get('pair')))} {fmt.name(best.get('strategy'))} "
                     f"{fmt.r(best.get('pnl_r'))} ({fmt.money(best.get('pnl'))})")
        lines.append(f"💀 {fmt.esc(fmt.coin(worst.get('pair')))} {fmt.name(worst.get('strategy'))} "
                     f"{fmt.r(worst.get('pnl_r'))} ({fmt.money(worst.get('pnl'))})")
        longs = summarise([t for t in trades if t.get('direction') == 'LONG'])
        shorts = summarise([t for t in trades if t.get('direction') == 'SHORT'])
        lines.append(f"Лонги {longs.get('trades', 0)} сд · {fmt.r(longs.get('sum_r'))} | "
                     f"шорты {shorts.get('trades', 0)} сд · {fmt.r(shorts.get('sum_r'))}")
    if total.get('skipped'):
        lines.append(f"<i>не в счёте: {total['skipped']} сд с дырой в свечах — как на сайте</i>")
    lines.append('<i>по стратегиям — с их последнего перезапуска отсчёта</i>')
    periods = [((('• ' if key == period else '') + label), f'st:{key}') for key, label in PERIODS]
    return '\n'.join(lines), keyboard([periods[:2], periods[2:], _nav(refresh=f'st:{period}')])


# ── Стратегии ────────────────────────────────────────────────────────────────

def _sides(value):
    return {'both': 'обе стороны', 'long': 'только лонги', 'short': 'только шорты'}.get(value, value or '—')


def strategies_view(d):
    strategies = d.get('strategies') or {}
    lines = ['🎛 <b>Стратегии</b>', fmt.RULE]
    buttons = []
    for n in _strategies(d):
        s, cfg = strategies[n], _settings_of(d, n)
        on = cfg.get('enabled', True)
        opened = sum(1 for p in d.get('open') or [] if p.get('strategy') == n)
        waiting = sum(1 for o in d.get('pending') or [] if o.get('strategy') == n)
        lines.append(f"{'🟢' if on else '⏸'} <b>{fmt.name(n)}</b> · "
                     f"{'входы вкл' if on else 'ВХОДЫ ВЫКЛ'} · риск {fmt.num(cfg.get('risk_pct')):g}% · "
                     f"{_sides(cfg.get('sides'))}")
        lines.append(f"   {fmt.money(s.get('equity'), signed=False)} ({fmt.pct(s.get('return_pct'))}) · "
                     f"позиций {opened} · заявок {waiting}"
                     + ('' if cfg.get('notify', True) else ' · 🔕'))
        buttons.append((f"{'🟢' if on else '⏸'} {fmt.name(n)}", f's:{n}'))
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append(_nav(refresh='sl'))
    return '\n'.join(lines), keyboard(rows)


def strategy_view(d, name):
    strategies = d.get('strategies') or {}
    if name not in strategies:
        return f'Стратегии {fmt.esc(name)} нет.', keyboard([_nav(back=('◀ Стратегии', 'sl'))])
    s, cfg = strategies[name], _settings_of(d, name)
    on = cfg.get('enabled', True)
    notify = cfg.get('notify', True)
    open_ = [p for p in d.get('open') or [] if p.get('strategy') == name]
    waiting = [o for o in d.get('pending') or [] if o.get('strategy') == name]
    day = summarise(_closed(d, name, _since(d, 'd')))
    week = summarise(_closed(d, name, _since(d, '7')))
    lines = [
        f"{'🟢' if on else '⏸'} <b>{fmt.name(name)}</b> · {'входы включены' if on else 'входы ВЫКЛЮЧЕНЫ'}",
        fmt.RULE,
        f"Капитал {fmt.money(s.get('equity'), signed=False)} · старт "
        f"{fmt.money(s.get('start_balance'), signed=False)} · {fmt.pct(s.get('return_pct'))}",
        f"Позиций {len(open_)}"
        + (f" · плавает {fmt.money(sum(fmt.num(p.get('unrealised')) for p in open_))}" if open_ else '')
        + f" · заявок {len(waiting)}",
        f"Сегодня {fmt.money(day.get('pnl'))} ({day.get('trades', 0)} сд) · 7 дней "
        f"{fmt.money(week.get('pnl'))} ({week.get('trades', 0)} сд, {fmt.r(week.get('sum_r'))})",
        f"Риск на сделку {fmt.num(cfg.get('risk_pct')):g}% · {_sides(cfg.get('sides'))}",
        f"Сделки в Telegram: {'присылать' if notify else 'не присылать'}",
    ]
    if s.get('reset_at'):
        lines.append(f"<i>отсчёт с {fmt.clock(s['reset_at'])}</i>")
    if not on:
        lines.append('<i>выключены только новые входы: позиции и заявки ведутся как обычно</i>')
    rows = [
        [('⏸ Выключить входы', f'se:{name}') if on else ('▶️ Включить входы', f'se:{name}')],
        [('🔕 Не присылать сделки', f'sn:{name}') if notify else ('🔔 Присылать сделки', f'sn:{name}')],
        [(f'❌ Закрыть всё ({len(open_)} поз., {len(waiting)} заяв.)', f'sa:{name}')
         if (open_ or waiting) else None],
        _nav(back=('◀ Стратегии', 'sl'), refresh=f's:{name}'),
    ]
    return '\n'.join(lines), keyboard(rows)


def strategy_off_confirm_view(d, name, token):
    return ('\n'.join([
        f"⏸ <b>Выключить входы {fmt.name(name)}?</b>",
        fmt.RULE,
        'Новые сделки открываться не будут. Открытые позиции и заявки ведутся как обычно.',
        'Включить обратно — одной кнопкой; на сайте это тот же тумблер.',
    ]), keyboard([[('✅ Выключить', f'y:f:{token}:{name}'), ('↩️ Нет', f's:{name}')]]))


def close_all_confirm_view(d, name, token):
    open_ = [p for p in d.get('open') or [] if p.get('strategy') == name]
    waiting = [o for o in d.get('pending') or [] if o.get('strategy') == name]
    total = sum(fmt.num(p.get('unrealised')) for p in open_)
    return ('\n'.join([
        f"❌ <b>Закрыть всё у {fmt.name(name)}?</b>",
        fmt.RULE,
        f"Позиций {len(open_)} — по рынку, итог ≈ {fmt.money(total)}; заявок {len(waiting)} — снять.",
        'Отменить будет нельзя. Входы стратегии останутся включены.',
    ]), keyboard([[('✅ Закрыть всё', f'y:a:{token}:{name}'), ('↩️ Нет', f's:{name}')]]))


# ── Уведомления ──────────────────────────────────────────────────────────────

def notify_view(d):
    notify = (d.get('settings') or {}).get('NOTIFY') or {}

    def on(event):
        return notify.get(f'{event}_telegram', True) is not False

    lines = [f"🔔 <b>Уведомления</b> · {'🔇 звук выключен — не приходит ничего' if d.get('muted') else 'звук включён'}",
             fmt.RULE]
    for event, label, what in EVENTS:
        lines.append(f"{'✅' if on(event) else '▫️'} <b>{label}</b> — {what}")
    lines.append('')
    lines.append('<b>Сделки каких стратегий присылать:</b> '
                 + ', '.join(f"{fmt.name(n)} {'✅' if _settings_of(d, n).get('notify', True) else '▫️'}"
                             for n in _strategies(d)))
    lines.append('<i>нажатие переключает; на сайте — те же настройки</i>')
    toggles = [(f"{'✅' if on(e) else '▫️'} {label}", f'ne:{e}') for e, label, _ in EVENTS]
    rows = [toggles[i:i + 2] for i in range(0, len(toggles), 2)]
    strat = [(f"{'✅' if _settings_of(d, n).get('notify', True) else '▫️'} {fmt.name(n)}", f'ns:{n}')
             for n in _strategies(d)]
    rows += [strat[i:i + 3] for i in range(0, len(strat), 3)]
    rows.append([('🔔 Включить звук' if d.get('muted') else '🔇 Выключить всё', 'nm')])
    rows.append(_nav(refresh='nt'))
    return '\n'.join(lines), keyboard(rows)


# ── Итог действия и справка ──────────────────────────────────────────────────

def result_view(ok, message, back):
    icon = '✅' if ok else '⚠️'
    return f"{icon} {fmt.esc(message)}", keyboard([_nav(back=back)])


def help_view():
    return ('\n'.join([
        '🤖 <b>Kraken — как пользоваться</b>',
        fmt.RULE,
        '/menu — панель: капитал, сегодня, стратегии',
        '/positions — позиции: карточка, безубыток, закрыть',
        '/orders — заявки: карточка, снять',
        '/setups — сетапы ИИ: ждут условия, ждут цену, в позиции',
        '/stats — статистика по стратегиям за период',
        '/strategies — включить/выключить входы стратегии',
        '/notify — какие уведомления присылать',
        '/pause, /resume — новые входы стоп/старт (позиции ведутся)',
        '/mute, /unmute — выключить/включить все уведомления',
        '/export — журнал сделок файлами',
        '',
        'Все действия, которые меняют позиции, — только с подтверждением. '
        'Увеличить риск отсюда нельзя: закрыть, снять, стоп в безубыток, пауза.',
    ]), keyboard([_nav()]))
