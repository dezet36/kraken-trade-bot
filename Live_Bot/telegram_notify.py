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
    в мульти-тенант режиме передаётся telegram_id конкретного юзера."""
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
    chat_id=None -> legacy общий чат; иначе telegram_id конкретного юзера."""
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


def positions_restored(recovered: list, telegram_id=None):
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
        chat_id=telegram_id,
    )


def limit_order_placed(pair: str, side: str, limit_price: float, stop_loss: float,
                       max_hours: float, telegram_id=None, signal: dict = None, df_1h=None):
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
            if chart_path and _send_photo(chart_path, caption=text, chat_id=telegram_id):
                return
        except Exception as e:
            log(f"limit_order_placed: ошибка графика — {e}")

    _send(text, chat_id=telegram_id)


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


def trade_opened(signal: dict, df_1h=None, telegram_id=None):
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
        sl_ctx    = "ниже зоны A" if is_long else "выше зоны A"
    elif trigger['zone'] == 'Zone_B':
        zone_desc = "Глубокая коррекция (78.6%–88.6%)"
        sl_ctx    = "ниже старта импульса" if is_long else "выше старта импульса"
    else:
        # Вход между зонами. Называем глубину отката числом: выдавать это за
        # зону B, как делала прежняя ветка `else`, значит писать неправду.
        _retr = (abs(setup['end_price'] - params['entry']) / setup['size'] * 100
                 if setup.get('size') else 0)
        zone_desc = f"Коррекция {_retr:.1f}% — между зонами"
        sl_ctx    = "ниже зоны A" if is_long else "выше зоны A"

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
            if chart_path and _send_photo(chart_path, caption=text, chat_id=telegram_id):
                return  # photo sent — no need for plain text
        except Exception as e:
            log(f"trade_opened: ошибка графика — {e}")

    # Fallback: plain text message
    _send(text, chat_id=telegram_id)


def tp_hit(pair: str, tp_num: int, direction: str, price: float,
           remaining_pct: int, realized_pnl: float = 0.0, telegram_id=None):
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
        chat_id=telegram_id,
    )


def trail_activated(pair: str, direction: str, trail_level: float):
    dir_s = "LONG" if direction == "LONG" else "SHORT"
    _send(
        f"<b>🔄 Трейлинг активирован — {pair} {dir_s}</b>\n"
        f"Стартовый стоп: <code>${trail_level:.4f}</code>\n"
        f"Прибыль с TP2 защищена"
    )


def trade_closed(pair: str, direction: str, reason: str, pnl: float,
                 daily_pnl: float, balance: float,
                 entry: float = 0.0, exit_price: float = 0.0,
                 duration_min: int = 0, tps_hit: int = 0, telegram_id=None):
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
        chat_id=telegram_id,
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


def error_alert(message: str, telegram_id=None):
    if not _allowed('error'):
        return
    _send(
        f"⚠️ <b>ОШИБКА БОТА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<code>{message[:400]}</code>\n"
        f"⏰ {_now()}",
        chat_id=telegram_id,
    )


# ── Подписка (платформа) ───────────────────────────────────────────────────────

def subscription_expiring(days: int, until_str: str, telegram_id=None):
    """Напоминание за N дней до окончания доступа."""
    _send(
        f"⏳ <b>Подписка скоро истекает</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Осталось: <b>{days} дн.</b> (до {until_str})\n"
        f"Продли заранее, чтобы бот не перестал открывать новые сделки.\n"
        f"Оплата: /pay  ·  Статус: /subscription",
        chat_id=telegram_id,
    )


def subscription_expired(telegram_id=None):
    """Уведомление в момент окончания доступа."""
    _send(
        f"⛔ <b>Подписка истекла</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Новые сделки больше не открываются. Уже открытые позиции продолжают вестись\n"
        f"до их TP/SL — принудительно не закрываем.\n"
        f"Возобновить доступ: /pay",
        chat_id=telegram_id,
    )


def subscription_extended(days_added: int, until_str: str, telegram_id=None):
    """Уведомление о продлении подписки (оплата или админ-грант)."""
    _send(
        f"✅ <b>Подписка продлена</b> на {days_added} дн.\n"
        f"Доступ до: <b>{until_str}</b>\n"
        f"Спасибо! 🚀",
        chat_id=telegram_id,
    )
