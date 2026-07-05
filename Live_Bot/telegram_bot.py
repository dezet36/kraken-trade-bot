"""
Telegram control panel for Kraken bot.
Runs as a background daemon thread using long-polling (pure requests).

Commands:
  /status    — баланс, позиции, дневной PnL
  /positions — открытые позиции с live PnL
  /stats     — полная статистика из журнала
  /close PAIR — ручное закрытие позиции
  /pause     — блокировать новые входы
  /resume    — возобновить торговлю
  /mute      — отключить уведомления
  /unmute    — включить уведомления
  /help      — список команд
"""

import threading
import time
import requests
import config
from logger import log
from datetime import datetime


class BotController:
    def __init__(self):
        self._paused  = False
        self._muted   = False
        self._lock    = threading.Lock()
        self._offset  = 0
        self._running = False
        self._thread  = None
        self.trade_manager = None  # assigned by bot.py after LiveTradeManager() init

    # ── public API ─────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True, name="tg-panel")
        self._thread.start()
        self._set_commands()
        log("Telegram-панель запущена (polling)")

    def stop(self):
        self._running = False

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def is_muted(self) -> bool:
        with self._lock:
            return self._muted

    # ── polling ────────────────────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                self._fetch_updates()
            except Exception as e:
                log(f"Telegram polling error: {e}")
            time.sleep(1)

    def _fetch_updates(self):
        if not config.TELEGRAM_BOT_TOKEN:
            time.sleep(30)
            return
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
        try:
            resp = requests.get(url, params={"offset": self._offset, "timeout": 5}, timeout=10)
        except Exception:
            return
        if not resp.ok:
            return
        for update in resp.json().get("result", []):
            self._offset = update["update_id"] + 1
            try:
                self._handle_update(update)
            except Exception as e:
                log(f"Telegram update handler error: {e}")

    # ── update routing ─────────────────────────────────────────────────────────

    def _handle_update(self, update):
        if "message" in update:
            msg     = update["message"]
            chat_id = str(msg["chat"]["id"])
            if chat_id != str(config.TELEGRAM_CHAT_ID):
                return
            text = msg.get("text", "").strip()
            if text.startswith("/"):
                parts = text.split()
                cmd   = parts[0].lower().split("@")[0]
                args  = parts[1:] if len(parts) > 1 else []
                self._handle_command(cmd, chat_id, args)

        elif "callback_query" in update:
            cb      = update["callback_query"]
            chat_id = str(cb["message"]["chat"]["id"])
            if chat_id != str(config.TELEGRAM_CHAT_ID):
                return
            self._answer_callback(cb["id"])
            self._handle_command(cb["data"], chat_id, [])

    def _handle_command(self, cmd: str, chat_id: str, args: list = None):
        args = args or []
        if cmd in ("/start", "/status"):
            self._send_status(chat_id)
        elif cmd == "/pause":
            with self._lock:
                self._paused = True
            self._send(chat_id,
                "⏸ <b>Бот приостановлен</b>\n"
                "Новые входы заблокированы.\n"
                "Открытые позиции управляются.")
        elif cmd == "/resume":
            with self._lock:
                self._paused = False
            self._send(chat_id, "▶️ <b>Бот возобновлён</b>\nСканирование и новые входы активны.")
        elif cmd == "/positions":
            self._send_positions(chat_id)
        elif cmd == "/stats":
            self._send_stats(chat_id)
        elif cmd == "/close":
            if args:
                pair = args[0].upper()
                if not pair.endswith('USDT'):
                    pair += 'USDT'
                self._handle_close(pair, chat_id)
            else:
                tm = self.trade_manager
                open_pairs = sorted(tm.get_open_pairs()) if tm else []
                if open_pairs:
                    pairs_list = '\n'.join(f'  /close {p}' for p in open_pairs)
                    self._send(chat_id, f"Укажи пару для закрытия:\n{pairs_list}")
                else:
                    self._send(chat_id, "Нет открытых позиций.")
        elif cmd == "/mute":
            with self._lock:
                self._muted = True
            self._send(chat_id, "🔇 <b>Уведомления отключены</b>\nКоманды по-прежнему работают.")
        elif cmd == "/unmute":
            with self._lock:
                self._muted = False
            self._send(chat_id, "🔔 <b>Уведомления включены</b>")
        elif cmd == "/export":
            self._send_export(chat_id)
        elif cmd == "/help":
            self._send_help(chat_id)

    # ── message builders ───────────────────────────────────────────────────────

    def _send_status(self, chat_id: str):
        tm = self.trade_manager
        if tm is None:
            self._send(chat_id, "⚠️ Trade manager не инициализирован")
            return

        balance = tm.get_real_balance()
        active  = tm.get_active_count()
        pairs   = ", ".join(sorted(tm.get_open_pairs())) or "—"

        today = datetime.now().date()
        today_trades = [t for t in tm.trade_history
                        if t.get('exit_time') and t['exit_time'].date() == today]
        daily_pnl = sum(t.get('pnl', 0) for t in today_trades)
        wins      = sum(1 for t in today_trades if t.get('pnl', 0) > 0)
        total_t   = len(today_trades)
        wr_str    = f"{wins/total_t*100:.0f}%" if total_t > 0 else "—"

        with self._lock:
            paused = self._paused
            muted  = self._muted

        status_icon = "⏸" if paused else "▶️"
        mute_icon   = "🔇" if muted  else "🔔"
        mode        = "🟢 DEMO" if config.TRADING_MODE == "DEMO" else "🔴 LIVE"
        pnl_icon    = "📈" if daily_pnl >= 0 else "📉"
        pnl_sign    = "+" if daily_pnl >= 0 else ""
        now         = datetime.now().strftime("%H:%M %d.%m")

        text = (
            f"<b>📊 Kraken — {mode}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Состояние:    {status_icon} {'Пауза' if paused else 'Активен'}\n"
            f"Уведомления:  {mute_icon} {'Выкл' if muted else 'Вкл'}\n"
            f"Баланс:       <b>${balance:,.2f}</b>\n"
            f"Позиций:      {active}/{config.MAX_ACTIVE_PAIRS}\n"
            f"Пары:         {pairs}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Сегодня:      {total_t} сделок  ✅{wins} ❌{total_t-wins}  WR: {wr_str}\n"
            f"{pnl_icon} Дневной PnL: <b>{pnl_sign}${daily_pnl:.2f}</b>\n"
            f"⏰ {now}"
        )

        pause_btn = "▶️ Возобновить" if paused else "⏸ Пауза"
        pause_cmd = "/resume"       if paused else "/pause"
        mute_btn  = "🔔 Вкл звук"  if muted  else "🔇 Без звука"
        mute_cmd  = "/unmute"       if muted  else "/mute"

        keyboard = {"inline_keyboard": [
            [{"text": pause_btn, "callback_data": pause_cmd},
             {"text": mute_btn,  "callback_data": mute_cmd}],
            [{"text": "📋 Позиции",   "callback_data": "/positions"},
             {"text": "📊 Статистика","callback_data": "/stats"}],
            [{"text": "🔄 Обновить",  "callback_data": "/status"}],
        ]}
        self._send(chat_id, text, reply_markup=keyboard)

    def _send_positions(self, chat_id: str):
        tm = self.trade_manager
        if tm is None:
            self._send(chat_id, "⚠️ Trade manager не инициализирован")
            return

        open_positions = [
            (pair, pos)
            for pair, positions in tm.active_positions.items()
            for pos in positions
            if pos['status'] == 'OPEN'
        ]
        pending = tm._load_pending_orders()

        if not open_positions and not pending:
            self._send(chat_id, "📭 <b>Открытых позиций и ожидающих ордеров нет</b>")
            return

        lines = ([f"<b>📋 Открытые позиции ({len(open_positions)})</b>", "━━━━━━━━━━━━━━━━━━━━"]
                 if open_positions else [])
        for pair, pos in open_positions:
            direction = pos['signal']['setup']['type']
            entry     = pos['entry_price']
            sl        = pos['params']['stop_loss']
            tp1       = pos['params']['take_profit_1']
            tp2       = pos['params']['take_profit_2']
            size      = pos['params']['position_size']
            tps_hit   = pos['tp_hit']
            trail_ico = " 🔄" if pos.get('trailing_active') else ""
            be_ico    = " ➿" if pos.get('breakeven_set') else ""
            dir_icon  = "📈" if direction == "LONG" else "📉"
            since     = pos['entry_time'].strftime("%H:%M %d.%m")

            # Duration
            minutes = int((datetime.now() - pos['entry_time']).total_seconds() / 60)
            h, m = minutes // 60, minutes % 60
            dur_str = f"{h}ч {m}м" if h > 0 else f"{m}м"

            # Live PnL (try to fetch current price)
            pnl_str = ""
            try:
                ticker = tm.exchange.fetch_ticker(pair)
                cur    = ticker['last']
                if direction == 'LONG':
                    unreal = (cur - entry) * size
                else:
                    unreal = (entry - cur) * size
                pnl_icon = "🟢" if unreal >= 0 else "🔴"
                pnl_str  = f"\n   PnL:    {pnl_icon} <b>${unreal:+.2f}</b>  @ <code>${cur:.4f}</code>"
            except Exception:
                pass

            n_tp = len(getattr(config, 'TP_CLOSE_FRACTIONS', [1.0]))
            tp_line = (f"   TP:     <code>${tp1:.4f}</code>" if n_tp == 1 else
                       f"   TP1:    <code>${tp1:.4f}</code>  TP2: <code>${tp2:.4f}</code>")
            lines.append(
                f"\n{dir_icon} <b>{pair}</b> {direction}{trail_ico}{be_ico}  [{dur_str}]\n"
                f"   Вход:   <code>${entry:.4f}</code>{pnl_str}\n"
                f"   SL:     <code>${sl:.4f}</code>\n"
                f"{tp_line}\n"
                f"   TP взято: {tps_hit}/{n_tp}"
            )

        if pending:
            if lines:
                lines.append("")
            lines.append(f"<b>⏳ Ожидают заполнения ({len(pending)})</b>")
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            for pair, po in pending.items():
                side = "LONG" if po.get('side') == 'buy' else "SHORT"
                lines.append(f"⏳ <b>{pair}</b> {side} — лимит "
                             f"<code>${po.get('limit_price', 0):.4f}</code>")

        self._send(chat_id, "\n".join(lines))

    def _send_stats(self, chat_id: str):
        tm = self.trade_manager
        if tm is None:
            self._send(chat_id, "⚠️ Trade manager не инициализирован")
            return

        stats = tm.get_stats_dict()
        if not stats:
            self._send(chat_id, "📊 Статистика пуста — сделок ещё не было.\nДанные появятся после первой закрытой сделки.")
            return

        total = stats['total']
        wins  = stats['wins']
        wr    = stats['win_rate']
        pf    = stats['profit_factor']
        pf_str = f"{pf:.2f}" if pf != float('inf') else "∞"

        total_pnl = stats['total_pnl']
        today_pnl = stats['today_pnl']
        best      = stats['best']
        worst     = stats['worst']
        avg_min   = stats['avg_duration']

        avg_h, avg_m = int(avg_min // 60), int(avg_min % 60)
        dur_str = f"{avg_h}ч {avg_m}м" if avg_h > 0 else f"{avg_m}м"

        za_cnt, za_w = stats['zone_a']
        zb_cnt, zb_w = stats['zone_b']
        l_cnt,  l_w  = stats['longs']
        s_cnt,  s_w  = stats['shorts']

        za_wr = za_w / za_cnt * 100 if za_cnt > 0 else 0
        zb_wr = zb_w / zb_cnt * 100 if zb_cnt > 0 else 0
        l_wr  = l_w  / l_cnt  * 100 if l_cnt  > 0 else 0
        s_wr  = s_w  / s_cnt  * 100 if s_cnt  > 0 else 0

        pnl_icon  = "📈" if total_pnl >= 0 else "📉"
        today_ico = "📈" if today_pnl >= 0 else "📉"

        self._send(chat_id,
            f"<b>📊 Статистика — Kraken</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 Сделок:        <b>{total}</b>\n"
            f"✅ Прибыльных:    <b>{wins}</b> ({wr:.1f}%)\n"
            f"❌ Убыточных:     <b>{stats['losses']}</b> ({100-wr:.1f}%)\n"
            f"📊 Profit Factor: <b>{pf_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{pnl_icon} Total PnL:   <b>${total_pnl:+.2f}</b>\n"
            f"{today_ico} Сегодня:     <b>${today_pnl:+.2f}</b>\n"
            f"⏱ Ср. сделка:  {dur_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 Лучшая:  <code>${best[0]:+.2f}</code>  {best[1]}\n"
            f"📉 Худшая:  <code>${worst[0]:+.2f}</code>  {worst[1]}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🅰️ Zone A:  {za_cnt} сд.  WR: {za_wr:.0f}%\n"
            f"🅱️ Zone B:  {zb_cnt} сд.  WR: {zb_wr:.0f}%\n"
            f"📈 LONG:    {l_cnt} сд.   WR: {l_wr:.0f}%\n"
            f"📉 SHORT:   {s_cnt} сд.  WR: {s_wr:.0f}%"
        )

    def _handle_close(self, pair: str, chat_id: str):
        tm = self.trade_manager
        if tm is None:
            self._send(chat_id, "⚠️ Trade manager не инициализирован")
            return

        open_pairs = tm.get_open_pairs()
        if pair not in open_pairs:
            pairs_str = ", ".join(sorted(open_pairs)) or "нет открытых позиций"
            self._send(chat_id,
                f"❓ Позиция <b>{pair}</b> не найдена.\n"
                f"Открытые: {pairs_str}")
            return

        self._send(chat_id, f"⏳ Закрываю <b>{pair}</b>...")
        success, price = tm.close_position_by_pair(pair)
        if success:
            self._send(chat_id,
                f"✅ <b>{pair}</b> закрыта вручную\n"
                f"Цена: <code>${price:.4f}</code>")
        else:
            self._send(chat_id, f"⚠️ Не удалось закрыть <b>{pair}</b>. Проверь лог.")

    def _send_help(self, chat_id: str):
        self._send(chat_id,
            "<b>🤖 Kraken — команды</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/status         — баланс и состояние\n"
            "/positions      — позиции с live PnL\n"
            "/stats          — полная статистика\n"
            "/close BTCUSDT  — закрыть позицию вручную\n"
            "/pause          — остановить новые входы\n"
            "/resume         — возобновить торговлю\n"
            "/mute           — отключить уведомления\n"
            "/unmute         — включить уведомления\n"
            "/export         — выгрузить журнал сделок файлами (для анализа)\n"
            "/help           — эта справка")

    def _send_export(self, chat_id: str):
        """Шлёт файлы журнала сделок документами в чат (для анализа)."""
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        files = [os.path.join(base, 'trades_detail.jsonl'),
                 os.path.join(base, 'trades_journal.csv')]
        sent = 0
        for path in files:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                if self._send_document(chat_id, path):
                    sent += 1
        if sent == 0:
            self._send(chat_id, "📭 Журнал пока пуст — файлы появятся после первой закрытой сделки.")
        else:
            self._send(chat_id, f"📎 Выгружено файлов: {sent}. Передай их для анализа сделок.")

    # ── low-level HTTP ─────────────────────────────────────────────────────────

    def _send_document(self, chat_id: str, path: str) -> bool:
        if not config.TELEGRAM_BOT_TOKEN:
            return False
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendDocument"
            with open(path, 'rb') as f:
                r = requests.post(url, data={"chat_id": chat_id},
                                  files={"document": f}, timeout=60)
            return bool(r.json().get('ok'))
        except Exception as e:
            log(f"Telegram sendDocument error: {e}")
            return False

    def _send(self, chat_id: str, text: str, reply_markup: dict = None):
        if not config.TELEGRAM_BOT_TOKEN:
            return
        try:
            url     = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            log(f"Telegram panel send error: {e}")

    def _answer_callback(self, callback_id: str):
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
            requests.post(url, json={"callback_query_id": callback_id}, timeout=5)
        except Exception:
            pass

    def _set_commands(self):
        if not config.TELEGRAM_BOT_TOKEN:
            return
        commands = [
            {"command": "status",    "description": "Баланс, позиции, дневной PnL"},
            {"command": "positions", "description": "Открытые позиции с live PnL"},
            {"command": "stats",     "description": "Полная статистика торговли"},
            {"command": "close",     "description": "Закрыть позицию: /close BTCUSDT"},
            {"command": "pause",     "description": "Остановить новые входы"},
            {"command": "resume",    "description": "Возобновить торговлю"},
            {"command": "mute",      "description": "Отключить уведомления"},
            {"command": "unmute",    "description": "Включить уведомления"},
            {"command": "help",      "description": "Список команд"},
        ]
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/setMyCommands"
            resp = requests.post(url, json={"commands": commands}, timeout=10)
            if resp.ok:
                log("Telegram команды зарегистрированы")
            else:
                log(f"Telegram setMyCommands: {resp.text[:100]}")
        except Exception as e:
            log(f"Telegram setMyCommands error: {e}")


# Global singleton — imported by bot.py and telegram_notify.py
controller = BotController()
