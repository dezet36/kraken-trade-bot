"""
Telegram notification module.
All functions are silent-fail — a Telegram error never crashes the bot.
Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env to enable.
"""

import os
import requests
import config
from exit_plan import tp_plan
from logger import log
from datetime import datetime

# ── internal send ─────────────────────────────────────────────────────────────

def _send(text: str, chat_id=None) -> bool:
    """Отправка сообщения. chat_id=None -> legacy общий чат (config.TELEGRAM_CHAT_ID);
    адресат — общий чат из .env."""
    target = chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not target:
        return False
    # Глобальный mute касается только legacy общего чата (per-user mute — фаза C)
    if chat_id is None:
        try:
            from telegram_bot import controller
            if controller.is_muted():
                return False
        except Exception:
            pass
    try:
        url  = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": target, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            log(f"Telegram: ошибка отправки — {resp.status_code} {resp.text[:120]}")
        return resp.ok
    except Exception as e:
        log(f"Telegram: исключение — {e}")
        return False


def _now() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%H:%M %d.%m UTC")


def _pnl_str(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:.2f}"


def _dur_str(minutes: int) -> str:
    if minutes <= 0:
        return "—"
    h = minutes // 60
    m = minutes % 60
    return f"{h}ч {m}м" if h > 0 else f"{m}м"


def _send_photo(photo_path: str, caption: str = "", chat_id=None) -> bool:
    """Send a photo file via Telegram sendPhoto. Deletes the file afterwards.
    chat_id=None -> общий чат из .env."""
    target = chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not target:
        return False
    if chat_id is None:
        try:
            from telegram_bot import controller
            if controller.is_muted():
                return False
        except Exception:
            pass
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as f:
            resp = requests.post(
                url,
                data={
                    "chat_id":    target,
                    "caption":    caption[:1024],
                    "parse_mode": "HTML",
                },
                files={"photo": f},
                timeout=30,
            )
        if not resp.ok:
            log(f"Telegram photo: ошибка — {resp.status_code} {resp.text[:120]}")
        return resp.ok
    except Exception as e:
        log(f"Telegram photo: исключение — {e}")
        return False
    finally:
        try:
            os.remove(photo_path)
        except Exception:
            pass


# ── public notifications ───────────────────────────────────────────────────────

def bot_started(balance: float):
    mode = "🟢 DEMO" if config.TRADING_MODE == "DEMO" else "🔴 LIVE"
    _send(
        f"<b>🤖 Kraken запущен</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Режим:         {mode}\n"
        f"Баланс:        <b>${balance:,.2f}</b>\n"
        f"Пул пар:       {len(config.TRADING_PAIRS_POOL)} пар\n"
        f"Макс. позиций: {config.MAX_ACTIVE_PAIRS}\n"
        f"Риск/сделка:   {config.RISK_PER_TRADE}%\n"
        f"⏰ {_now()}"
    )


def positions_restored(recovered: list):
    """W7: уведомление о восстановленных позициях после перезапуска."""
    if not recovered:
        return
    lines = []
    for r in recovered:
        be_str    = "✓" if r.get('breakeven_set') else "✗"
        trail_str = "✓" if r.get('trailing_active') else "✗"
        lines.append(
            f"• {r['pair']} {r['direction']}  "
            f"(TP: {r['tp_hit']}  BE: {be_str}  Trail: {trail_str})"
        )
    _send(
        f"<b>🔄 Восстановлены позиции после перезапуска</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) +
        f"\n⏰ {_now()}",
    )


def limit_order_placed(pair: str, side: str, limit_price: float, stop_loss: float,
                       max_hours: float, signal: dict = None, df_1h=None):
    """W9+: GTC лимитный ордер выставлен, мониторинг до N часов.
    Если переданы signal+df_1h — прикладывает график сетапа (импульс, зоны, вход/SL/TP)."""
    dir_s = "LONG" if side == 'buy' else "SHORT"
    text = (
        f"<b>⏳ GTC Лимит выставлен — {pair} {dir_s}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Лимит: <code>${_fmt_p(limit_price)}</code>\n"
        f"Стоп:  <code>${_fmt_p(stop_loss)}</code>\n"
        f"Ждём заполнения до {max_hours:.0f}ч (без market fallback)\n"
        f"⏰ {_now()}"
    )

    # График сетапа прямо в момент постановки лимита (df_1h уже есть в execute_trade)
    if signal is not None and df_1h is not None:
        try:
            from chart_generator import generate_trade_chart
            chart_path = generate_trade_chart(signal, df_1h)
            if chart_path and _send_photo(chart_path, caption=text):
                return
        except Exception as e:
            log(f"limit_order_placed: ошибка графика — {e}")

    _send(text)


def bot_stopped(trade_count: int, daily_pnl: float, balance: float):
    _send(
        f"<b>🛑 Kraken остановлен</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Сделок за сессию: {trade_count}\n"
        f"PnL за сессию:    <b>{_pnl_str(daily_pnl)}</b>\n"
        f"Баланс:           <b>${balance:,.2f}</b>\n"
        f"⏰ {_now()}"
    )


def _fmt_p(p: float) -> str:
    """Adaptive price formatter — works for BTC ($60k) and SHIB ($0.000012)."""
    if p >= 1000:   return f"{p:.2f}"
    if p >= 1:      return f"{p:.4f}"
    if p >= 0.01:   return f"{p:.6f}"
    if p >= 0.0001: return f"{p:.8f}"
    return f"{p:.10f}"


def _fmt_ts(ts) -> str:
    """Format a timestamp to dd.mm HH:MM UTC."""
    try:
        return ts.strftime('%d.%m %H:%M UTC')
    except Exception:
        try:
            return str(ts)[:16].replace('T', ' ') + ' UTC'
        except Exception:
            return '—'


def trade_opened(signal: dict, df_1h=None):
    setup   = signal["setup"]
    trigger = signal["trigger"]
    params  = signal["params"]
    pair    = signal["trading_pair"]
    htf     = signal.get("htf_trend", "—")
    mode    = "🟢 DEMO" if config.TRADING_MODE == "DEMO" else "🔴 LIVE"
    is_long = setup["type"] == "LONG"

    direction_icon = "📈 LONG" if is_long else "📉 SHORT"
    # Третий случай существует: вход может стоять между зонами. Раньше здесь
    # было «A или B», и всё, что не A, объявлялось зоной B — включая то, что
    # ею не является.
    zone_icon      = {"Zone_A": "🅰️", "Zone_B": "🅱️"}.get(trigger["zone"], "◽")
    htf_icon       = "↗️" if htf == "BULLISH" else ("↘️" if htf == "BEARISH" else "↔️")

    impulse_pct = round(setup['size'] / setup['end_price'] * 100, 1)
    sl_dist_pct = abs(params['entry'] - params['stop_loss']) / params['entry'] * 100
    sl_dir      = "-" if is_long else "+"

    vol_ratio = trigger.get('volume_ratio', 0)
    vol_str   = f"  📊 {vol_ratio:.1f}×" if vol_ratio > 0 else ""

    # Impulse A/B prices and period
    a_price = setup['start_price']   # HIGH for SHORT, LOW for LONG
    b_price = setup['end_price']     # LOW for SHORT, HIGH for LONG
    imp_start = _fmt_ts(setup.get('start_time', '—'))
    imp_end   = _fmt_ts(setup.get('end_time',   '—'))

    if is_long:
        imp_line = (f"A(LOW)  <code>${_fmt_p(a_price)}</code>  {imp_start}\n"
                    f"   B(HIGH) <code>${_fmt_p(b_price)}</code>  {imp_end}  +{impulse_pct}%")
    else:
        imp_line = (f"A(HIGH) <code>${_fmt_p(a_price)}</code>  {imp_start}\n"
                    f"   B(LOW)  <code>${_fmt_p(b_price)}</code>  {imp_end}  -{impulse_pct}%")

    # Zone context
    if trigger['zone'] == 'Zone_A':
        zone_desc = "Классич. коррекция (38.2%–61.8%)"
    elif trigger['zone'] == 'Zone_B':
        zone_desc = "Глубокая коррекция (78.6%–88.6%)"
    else:
        # Вход между зонами. Называем глубину отката числом: выдавать это за
        # зону B, как делала прежняя ветка `else`, значит писать неправду.
        _retr = (abs(setup['end_price'] - params['entry']) / setup['size'] * 100
                 if setup.get('size') else 0)
        zone_desc = f"Коррекция {_retr:.1f}% — между зонами"

    # Цели берём из плана ЭТОЙ сделки: у стратегий он разный, и глобальная
    # настройка показывала бы SMC один тейк вместо трёх.
    plan_targets, plan_fractions = tp_plan(params)
    if len(plan_targets) == 1:
        tp1_pct = getattr(config, 'TP1_LEVEL', 0.25) * 100
        tp_block = f"🎯 Тейк (−{tp1_pct:.0f}%):  <code>${_fmt_p(plan_targets[0])}</code>\n"
    else:
        tp_block = ''.join(
            f"🎯 TP{i + 1}:  <code>${_fmt_p(price)}</code>  ({frac * 100:.0f}%)\n"
            for i, (price, frac) in enumerate(zip(plan_targets, plan_fractions)))

    text = (
        f"<b>{direction_icon} {pair}</b>  {zone_icon} {trigger['zone']}  {mode}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Импульс:\n"
        f"   {imp_line}\n"
        f"   {zone_desc}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Точка входа:  <code>${_fmt_p(params['entry'])}</code>\n"
        f"🛡 Стоп-лосс:    <code>${_fmt_p(params['stop_loss'])}</code>  ({sl_dir}{sl_dist_pct:.2f}%)\n"
        f"{tp_block}"
        + (f"⚖️ Безубыток при пробое B: <code>${_fmt_p(params['be_level'])}</code>\n"
           if params.get('be_level') else "")
        + f"━━━━━━━━━━━━━━━━━━━━\n"
        f"RR: 1:{params['rr']:.1f}  |  HTF: {htf_icon} {htf}{vol_str}\n"
        f"💰 Риск: ${params['risk_amount']:.2f}  |  Размер: {params['position_size']:.4f}\n"
        f"⏰ {_now()}"
    )

    # Try to send chart with the trade text as caption
    if df_1h is not None:
        try:
            from chart_generator import generate_trade_chart
            chart_path = generate_trade_chart(signal, df_1h)
            if chart_path and _send_photo(chart_path, caption=text):
                return  # photo sent — no need for plain text
        except Exception as e:
            log(f"trade_opened: ошибка графика — {e}")

    # Fallback: plain text message
    _send(text)


def tp_hit(pair: str, tp_num: int, direction: str, price: float,
           remaining_pct: int, realized_pnl: float = 0.0):
    icons   = {1: "🎯", 2: "🏆"}
    icon    = icons.get(tp_num, "🎯")
    dir_s   = "LONG" if direction == "LONG" else "SHORT"
    pnl_str = f"{_pnl_str(realized_pnl)}" if realized_pnl != 0 else "—"
    closed_pct = 100 - remaining_pct
    _send(
        f"{icon} <b>TP{tp_num} — {pair} {dir_s}</b>\n"
        f"Цена:          <code>${price:.4f}</code>\n"
        f"Закрыто:       {closed_pct}% позиции\n"
        f"Осталось:      {remaining_pct}%\n"
        f"Зафиксировано: <b>{pnl_str}</b>",
    )


def trade_closed(pair: str, direction: str, reason: str, pnl: float,
                 daily_pnl: float, balance: float,
                 entry: float = 0.0, exit_price: float = 0.0,
                 duration_min: int = 0, tps_hit: int = 0):
    dir_s = "LONG" if direction == "LONG" else "SHORT"

    n_tp = len(getattr(config, 'TP_CLOSE_FRACTIONS', [1.0]))
    reason_labels = {
        "TP2":      "🏆 Полный таргет (TP2)",
        "TP1":      ("🏆 Тейк-профит" if n_tp == 1 else "🎯 Частичный таргет (TP1)"),
        "BE":       "⚖️ Безубыток",
        "TRAIL_SL": "🔄 Трейлинг стоп",
        "TIME":     "⏱ Тайм-стоп (лимит удержания)",
        "Manual":   "🖐 Закрыто вручную",
        "EXT":      "🖐 Закрыто на бирже (вне бота)",
        "SL":       "🛑 Стоп-лосс",
    }
    reason_label = reason_labels.get(reason, f"❓ {reason}")

    pnl_icon = "💚" if pnl >= 0 else "🔴"
    entry_str = f"<code>${entry:.4f}</code>" if entry > 0 else "—"
    exit_str  = f"<code>${exit_price:.4f}</code>" if exit_price > 0 else "—"

    _send(
        f"{pnl_icon} <b>Закрыто — {pair} {dir_s}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{reason_label}\n"
        f"Вход:        {entry_str}\n"
        f"Выход:       {exit_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"PnL:         <b>{_pnl_str(pnl)}</b>\n"
        f"Время:       {_dur_str(duration_min)}\n"
        f"TP взято:    {tps_hit}/{n_tp}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Дневной PnL: <b>{_pnl_str(daily_pnl)}</b>\n"
        f"Баланс:      <b>${balance:,.2f}</b>\n"
        f"⏰ {_now()}",
    )


# ── Фантомный режим ──────────────────────────────────────────────────────────
# В фантоме уведомлений не было вовсе: trade_opened и trade_closed зовутся
# только из боевого пути, и месяц наблюдений шёл в Telegram молча — приходила
# одна дневная сводка без разбивки. А смысл фантома именно в сравнении
# стратегий, поэтому имя стратегии здесь стоит первым.

def _allowed(event: str) -> bool:
    """Разрешено ли это событие в канал «телеграм»."""
    try:
        import settings_store as settings
        return settings.notify_on(event, 'telegram')
    except Exception:                              # noqa: BLE001
        return True                                # настройка недоступна — не молчим


TRIGGER_TEXT = {
    'now': 'лимит на вход стоит сразу, ждёт цену',
    'close_above': 'закрытие часа выше {lvl}',
    'close_below': 'закрытие часа ниже {lvl}',
    'close_above_with_volume': 'закрытие часа выше {lvl} на объёме ≥ 1.5× медианы',
    'close_below_with_volume': 'закрытие часа ниже {lvl} на объёме ≥ 1.5× медианы',
    'retest': 'откат к {lvl} и закрытие, удержавшее его',
    'sweep_reclaim': 'вынос за {lvl} и возврат не позже 3 свечей',
}


def _level_kind(llm: dict, level_id):
    """Вид уровня по номеру: «пивот дня», «край ликвидаций шортов»…"""
    for lv in llm.get('levels') or []:
        if lv.get('id') == level_id:
            return (lv.get('kind') or '').split(' / ')[0]
    return ''


def llm_plan_caption(signal: dict, chart_span: str = '') -> str:
    """
    Подпись к картинке плана: только цифры, которые нужны, чтобы поставить
    сделку руками — вход, стоп, цели, условие. Всё «почему» — отдельным
    сообщением следом (llm_plan_story): подпись к фото в Telegram не длиннее
    1024 знаков, и объяснения в ней обрезались на полуслове.
    """
    llm = signal.get('llm') or {}
    params = signal.get('params') or {}
    pair = signal.get('trading_pair', '?')
    direction = (signal.get('setup') or {}).get('type', '?')
    entry = float(params.get('entry') or 0)
    stop = float(params.get('stop_loss') or 0)
    targets = [float(t) for t in (params.get('tp_targets') or []) if t]
    ids = llm.get('ids') or {}
    arrow = "🟢" if direction == "LONG" else "🔴"

    def pct(p):
        return f"{abs(p - entry) / entry * 100:.2f}%" if entry else "—"

    def tail(level_id):
        kind = _level_kind(llm, level_id)
        return f"  · {kind}" if kind else ''

    lines = [
        f"{arrow} <b>ИИ: план {pair} {direction}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Вход   <b>{_fmt_p(entry)}</b>{tail(ids.get('entry'))}",
        f"🛑 Стоп   <b>{_fmt_p(stop)}</b>  (−{pct(stop)}){tail(ids.get('stop'))}",
    ]
    tp_ids = ids.get('tp') or []
    for k, t in enumerate(targets, start=1):
        tid = tp_ids[k - 1] if k - 1 < len(tp_ids) else None
        lines.append(f"🎯 Цель {k} <b>{_fmt_p(t)}</b>  (+{pct(t)}){tail(tid)}")
    lines.append(f"R:R <b>{llm.get('rr') or params.get('rr') or '—'}</b> · вероятность "
                 f"{llm.get('p') or '—'} · факторы {llm.get('votes') or '—'}/5")
    when = llm.get('trigger_when') or 'now'
    lvl = _fmt_p(float(llm['trigger_level'])) if llm.get('trigger_level') else ''
    condition = TRIGGER_TEXT.get(when, when).format(lvl=lvl)
    import config
    ttl = int(getattr(config, 'LLM_TRIGGER_TTL_H', 12) or 12)
    lines.append(f"⏳ Условие: {condition} — ждёт до {ttl} ч")
    critic = llm.get('critic') or {}
    if critic.get('verdict') == 'confirm':
        lines.append("✅ Критик подтвердил" + (f" · риск: {critic['worst'][:120]}"
                                              if critic.get('worst') and critic.get('worst') not in ('—', 'нет') else ''))
    if chart_span:
        lines[0] += f"  <i>· {chart_span}</i>"
    return chr(10).join(lines)


# Предел подписи к фото в Telegram Bot API. Всё сообщение о плане живёт в
# ней — одно сообщение, а не картинка и текст порознь.
CAPTION_LIMIT = 1024


def _plain_len(text: str) -> int:
    """Длина без HTML-тегов: Telegram считает предел по видимому тексту."""
    import re
    return len(re.sub(r'<[^>]+>', '', text))


def llm_plan_story(signal: dict, budget: int = CAPTION_LIMIT) -> str:
    """
    Разбор плана по порядку чтения трейдера, ужатый в budget знаков:
    почему вход здесь → почему стоп здесь → почему цель здесь → где
    ликвидность → куда рынок → что сломает идею. Блоки идут по важности:
    когда не влезает, сначала пропадает риск, потом «куда рынок», потом
    ликвидность; вход, стоп и цель — всегда.
    """
    llm = signal.get('llm') or {}
    parts = llm.get('analysis_parts') or {}
    bias = {'up': 'вверх', 'down': 'вниз', 'flat': 'флэт'}.get(llm.get('bias') or '', '')
    # (заголовок, текст, предел знаков) — в порядке чтения; приоритет
    # отбрасывания — с конца.
    blocks = [
        ('📍 Почему вход здесь', llm.get('why'), 220),
        ('🛑 Почему стоп здесь', llm.get('stop_why'), 140),
        ('🎯 Почему цель здесь', llm.get('tp_why'), 140),
        ('💧 Где ликвидность', parts.get('liquidity'), 200),
        ('📈 Куда рынок' + (f' — {bias}' if bias else ''), parts.get('direction'), 160),
        ('⚠️ Что сломает идею', llm.get('risk'), 140),
    ]
    rendered = []
    for title, body, limit in blocks:
        body = ' '.join((body or '').split())
        if not body:
            continue
        if len(body) > limit:
            body = body[:limit - 1].rstrip() + '…'
        rendered.append(f"<b>{title}</b>" + chr(10) + body)
    while rendered and _plain_len(chr(10).join(rendered)) > budget:
        rendered.pop()
    return chr(10).join(rendered)


def llm_plan_message(signal: dict, chart_span: str = '') -> str:
    """Одно сообщение о плане: цифры, условие и разбор — в предел подписи к фото."""
    head = llm_plan_caption(signal, chart_span)
    story = llm_plan_story(signal, CAPTION_LIMIT - _plain_len(head) - 2)
    return head + (chr(10) + chr(10) + story if story else '')


def llm_setup_found(signal: dict, df_1h=None, frames=None):
    """
    План модели принят (прошёл проверки кода и критика): ОДНО сообщение —
    картинка, а в подписи цифры плана, условие и разбор «почему» по
    порядку чтения. Шлётся В МОМЕНТ ПРИНЯТИЯ ПЛАНА, а не при заполнении
    лимита: лимит может ждать цену полсуток, и человеку важно увидеть план
    тогда, когда он появился. О заполнении сообщит обычное «вход в сделку».

    frames(pair, tf, limit) -> df — откуда взять свечи другого таймфрейма.
    Какой таймфрейм показать, решает chart_frame по геометрии плана; без
    frames или при отказе биржи рисуются часовые, что на руках.
    """
    if not _allowed('llm_setup'):
        return False
    pair = signal.get('trading_pair', '?')
    if df_1h is not None:
        try:
            import chart_frame
            from chart_generator import generate_trade_chart
            price = float(df_1h['close'].iloc[-1]) if len(df_1h) else None
            tf, bars = chart_frame.pick(signal, price)
            df, shown = df_1h, '1h'
            if frames is not None and tf != '1h':
                try:
                    got = frames(pair, tf, bars)
                    if got is not None and len(got) >= 20:
                        df, shown = got, tf
                except Exception as exc:               # noqa: BLE001
                    log(f"Telegram: свечи {tf} для графика не пришли — {exc}; рисую часовые")
            text = llm_plan_message(signal, chart_frame.SPAN_TEXT.get(shown, shown))
            chart_path = generate_trade_chart(signal, df, timeframe=shown)
            if chart_path and _send_photo(chart_path, caption=text):
                return True
        except Exception as exc:                       # noqa: BLE001
            log(f"Telegram: график плана ИИ не собрался — {exc}")
    return _send(llm_plan_message(signal))


def llm_setups_text(setups: dict) -> str:
    """
    Список живых сетапов ИИ по кнопке: ждут условия → ждут цену → в позиции.
    Каждый — вход, стоп, цель, R:R, условие и сколько ещё живёт; «почему»
    одной строкой. Пусто — так и сказано.
    """
    armed, pending, open_ = setups.get('armed') or [], setups.get('pending') or [], setups.get('open') or []
    if not (armed or pending or open_):
        return ("🤖 <b>Живых сетапов ИИ нет</b>" + chr(10)
                + "Ни планов, ждущих условия, ни заявок, ни позиций.")

    def hours(minutes):
        minutes = int(minutes or 0)
        return f"{minutes // 60}ч {minutes % 60:02d}м" if minutes >= 60 else f"{minutes}м"

    def pct(a, b):
        return f"{abs(a - b) / b * 100:.2f}%" if b else '—'

    out = [f"🤖 <b>Живые сетапы ИИ</b> · {len(armed) + len(pending) + len(open_)}", "━━━━━━━━━━━━━━━━━━━━"]
    if armed:
        out.append(f"<b>⏳ Ждут условия ({len(armed)})</b>")
        for a in armed:
            icon = "🟢" if a.get('side') == 'LONG' else "🔴"
            entry, stop = float(a.get('entry') or 0), float(a.get('stop') or 0)
            targets = [float(t) for t in (a.get('targets') or []) if t]
            lvl = _fmt_p(float(a['level'])) if a.get('level') else ''
            cond = TRIGGER_TEXT.get(a.get('when') or 'now', a.get('when') or '').format(lvl=lvl)
            out.append(f"{icon} <b>{a['pair']} {a.get('side', '')}</b> · R:R {a.get('rr') or '—'} · p {a.get('p') or '—'}")
            out.append(f"   вход <b>{_fmt_p(entry)}</b> · стоп {_fmt_p(stop)} (−{pct(stop, entry)})"
                       + (f" · цель {_fmt_p(targets[0])} (+{pct(targets[0], entry)})" if targets else ''))
            out.append(f"   условие: {cond}")
            out.append(f"   ждёт {hours(a.get('minutes'))}, снимется через {hours(a.get('left_min'))}")
            if a.get('why'):
                out.append(f"   <i>{a['why'][:160]}</i>")
        out.append('')
    if pending:
        out.append(f"<b>📥 Заявка стоит, ждёт цену ({len(pending)})</b>")
        for o in pending:
            icon = "🟢" if o.get('direction') == 'LONG' else "🔴"
            entry = float(o.get('entry') or 0)
            out.append(f"{icon} <b>{o['pair']} {o.get('direction', '')}</b> · R:R {o.get('rr') or '—'}")
            out.append(f"   лимит <b>{_fmt_p(entry)}</b> · стоп {_fmt_p(float(o.get('stop') or 0))}"
                       + (f" · цель {_fmt_p(float(o['tp1']))}" if o.get('tp1') else ''))
            dist = f" · до лимита {o['distance_pct']}%" if o.get('distance_pct') is not None else ''
            out.append(f"   ждёт {hours(o.get('waiting_min'))}{dist}, снимется через {hours(o.get('expires_in_min'))}")
            if o.get('why'):
                out.append(f"   <i>{str(o['why'])[:160]}</i>")
        out.append('')
    if open_:
        out.append(f"<b>📈 В позиции ({len(open_)})</b>")
        for o in open_:
            icon = "🟢" if o.get('direction') == 'LONG' else "🔴"
            entry = float(o.get('entry') or 0)
            r = o.get('unrealised_r')
            out.append(f"{icon} <b>{o['pair']} {o.get('direction', '')}</b>"
                       + (f" · сейчас <b>{float(r):+.2f} R</b>" if r is not None else ''))
            out.append(f"   вход {_fmt_p(entry)} · стоп {_fmt_p(float(o.get('stop') or 0))}"
                       + (f" · цель {_fmt_p(float(o['tp1']))}" if o.get('tp1') else '')
                       + (f" · цена {_fmt_p(float(o['price']))}" if o.get('price') else ''))
            if o.get('why'):
                out.append(f"   <i>{str(o['why'])[:160]}</i>")
    return chr(10).join(out).rstrip()


def llm_setup_rejected(pair: str, side: str, entry: float, gate: str, detail: str = ''):
    """Модель предложила сетап, но его отклонил код или критик — коротко, без картинки."""
    if not _allowed('llm_setup'):
        return False
    return _send(
        f"⚪ <b>ИИ предложила сетап, отклонён</b> · {pair} {side} от {_fmt_p(entry)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{gate}" + (f": {detail[:300]}" if detail else '')
    )


def plan_dropped(strategy: str, pair: str, side: str, entry: float,
                 reason: str, detail: str = ''):
    """
    План или заявка умерли, не став сделкой.

    ЗАЧЕМ. Это единственное событие жизненного цикла, о котором не сообщали
    никому. План находился — приходило сообщение; план отклоняли — приходило;
    сделка открывалась и закрывалась — приходило. А «взведённый план не
    дождался условия за 12 часов» и «лимит не заполнен» уходили только в
    журнал. Со стороны это выглядело так, будто сетап растворился: 23.09.2026
    SUIUSDT висел одиннадцать часов и исчез из списка без единого слова.
    За четверо суток так умерли 47 заявок — то есть картина «что стало с
    планом» была неполной у каждой второй.
    """
    if not _allowed('plan_dropped'):
        return False
    arrow = "🟢" if str(side).upper() == "LONG" else "🔴"
    head = f"{arrow} <b>{pair} {side}</b>" if side else f"<b>{pair}</b>"
    return _send(
        f"⌛ <b>Сетап снят, сделки не было</b> · {strategy}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{head}" + (f" от {_fmt_p(entry)}" if entry else '') + "\n"
        f"{reason}" + (f"\n<i>{str(detail)[:300]}</i>" if detail else '')
    )


def paper_trade_opened(strategy: str, pair: str, direction: str, entry: float,
                       stop: float, target: float, rr: float, risk: float,
                       why: str = ''):
    if not _allowed('trade_opened'):
        return
    arrow = "🟢" if direction == "LONG" else "🔴"
    tail = f"\n<i>{why}</i>" if why else ""
    _send(
        f"{arrow} <b>{strategy}</b> · вход {pair} {direction}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Вход:  <b>{_fmt_p(entry)}</b>\n"
        f"Стоп:  {_fmt_p(stop)}\n"
        f"Цель:  {_fmt_p(target)}   RR {rr:.2f}\n"
        f"Риск:  ${risk:.2f}"
        + tail
    )


def paper_trade_closed(strategy: str, pair: str, reason: str, pnl: float,
                       pnl_r: float, balance: float):
    if not _allowed('trade_closed'):
        return
    icon = "✅" if pnl > 0 else ("⚪" if pnl == 0 else "❌")
    _send(
        f"{icon} <b>{strategy}</b> · закрыта {pair}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Итог:    <b>{_pnl_str(pnl)}</b>  ({pnl_r:+.2f} R)\n"
        f"Причина: {reason}\n"
        f"Депозит: <b>${balance:,.2f}</b>"
    )


def daily_by_strategy(rows: list, date_str: str = ''):
    """
    Дневная сводка С РАЗБИВКОЙ по стратегиям.

    Прежняя складывала всё в одну кучу: «12 сделок, +$40». По такой строке
    нельзя понять, что одна стратегия заработала, а вторая ровно столько же
    потеряла, — а ради этого сравнения фантом и запущен.

    rows: [{'strategy', 'pnl', 'pnl_r'}]
    """
    if not _allowed('daily'):
        return
    date_str = date_str or datetime.now().strftime("%d.%m.%Y")
    if not rows:
        _send(f"📊 <b>Итог за {date_str}</b>\nСделок не было")
        return

    by = {}
    for row in rows:
        item = by.setdefault(row.get('strategy') or '—',
                             {'n': 0, 'wins': 0, 'pnl': 0.0, 'r': 0.0})
        item['n'] += 1
        item['pnl'] += row.get('pnl', 0) or 0
        item['r'] += row.get('pnl_r', 0) or 0
        if (row.get('pnl', 0) or 0) > 0:
            item['wins'] += 1

    total_pnl = sum(i['pnl'] for i in by.values())
    total_n = sum(i['n'] for i in by.values())
    icon = "📈" if total_pnl >= 0 else "📉"

    lines = [f"{icon} <b>Итог за {date_str}</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for name, item in sorted(by.items(), key=lambda kv: -kv[1]['pnl']):
        wr = item['wins'] / item['n'] * 100 if item['n'] else 0
        lines.append(
            f"<b>{name}</b>: {item['n']} сд · WR {wr:.0f}% · "
            f"{_pnl_str(item['pnl'])} · {item['r']:+.2f} R")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Всего: {total_n} сд · <b>{_pnl_str(total_pnl)}</b>")
    _send("\n".join(lines))


def scan_result(liquid: int, candidates: int, active_positions: int):
    if candidates == 0:
        return
    _send(
        f"🔍 <b>Сканер</b> — {candidates} сетапов\n"
        f"Ликвидных пар: {liquid} / {len(config.TRADING_PAIRS_POOL)}\n"
        f"Позиций: {active_positions}/{config.MAX_ACTIVE_PAIRS}"
    )


def error_alert(message: str):
    if not _allowed('error'):
        return
    _send(
        f"⚠️ <b>ОШИБКА БОТА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{message[:400]}</code>\n"
        f"⏰ {_now()}",
    )


# ── Подписка (платформа) ───────────────────────────────────────────────────────

