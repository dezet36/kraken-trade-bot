"""
Мульти-юзерная Telegram-панель платформы (копи-трейдинг по подписке).

В отличие от legacy `telegram_bot.py` (один TELEGRAM_CHAT_ID) маршрутизирует по
`telegram_id` отправителя: каждый пользователь управляет ТОЛЬКО своим счётом и видит
ТОЛЬКО свою статистику.

Онбординг (/connect): выбор биржи → ввод API-ключа → ввод секрета → валидация на бирже →
шифрование (Fernet) → сохранение в БД → старт бесплатного триала. Сообщения с ключами
немедленно удаляются из чата.

Команды юзера: /start /connect /disconnect /status /positions /stats /close /pause
/resume /subscription /help. Долгий polling в фоновом потоке (как в legacy-панели).

Контроллер получает ссылку на PlatformManager (`controller.platform`) для доступа к
живым позициям/балансу активных подписчиков.
"""

import os
import threading
import time
import requests
from datetime import datetime, timezone

import config
import db
import crypto_security as crypto
import exchange as ex
import telegram_notify as tg
from trade_manager import compute_stats
from logger import log

EXCHANGE_LABELS = {'bybit': 'Bybit', 'bingx': 'BingX'}


def _fmt_p(p: float) -> str:
    if p >= 1000:   return f"{p:.2f}"
    if p >= 1:      return f"{p:.4f}"
    if p >= 0.01:   return f"{p:.6f}"
    if p >= 0.0001: return f"{p:.8f}"
    return f"{p:.10f}"


def _fmt_until(dt) -> str:
    if not dt:
        return "—"
    try:
        return dt.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    except Exception:
        return str(dt)


def _days_left(dt) -> int:
    if not dt:
        return 0
    try:
        return max(0, (dt - datetime.now(timezone.utc)).days)
    except Exception:
        return 0


class PlatformController:
    def __init__(self):
        self._offset  = 0
        self._running = False
        self._thread  = None
        self.platform = None              # назначается platform_bot.py (PlatformManager)
        self._conv    = {}                # telegram_id -> {'state', 'exchange', 'api_key'}

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True, name="tg-platform")
        self._thread.start()
        self._set_commands()
        log("Telegram-панель платформы запущена (polling)")

    def stop(self):
        self._running = False

    # ── polling ───────────────────────────────────────────────────────────────
    def _poll_loop(self):
        while self._running:
            try:
                self._fetch_updates()
            except Exception as e:
                log(f"Platform TG polling error: {e}")
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
                log(f"Platform TG handler error: {e}")

    # ── routing ───────────────────────────────────────────────────────────────
    def _handle_update(self, update):
        if "message" in update:
            msg     = update["message"]
            frm     = msg.get("from", {})
            user_id = frm.get("id")
            if not user_id:
                return
            chat_id    = msg["chat"]["id"]
            username   = frm.get("username") or frm.get("first_name")
            message_id = msg.get("message_id")
            text       = (msg.get("text") or "").strip()

            if text.startswith("/"):
                parts = text.split()
                cmd   = parts[0].lower().split("@")[0]
                args  = parts[1:]
                self._handle_command(cmd, user_id, chat_id, username, args)
            elif user_id in self._conv:
                # ввод в рамках диалога (ключ/секрет/депозит)
                self._handle_conversation_input(user_id, chat_id, message_id, text)
            else:
                # нажатие кнопки постоянного нижнего меню (текст совпадает с подписью)
                self._route_menu_text(text, user_id, chat_id, username)

        elif "callback_query" in update:
            cb      = update["callback_query"]
            frm     = cb.get("from", {})
            user_id = frm.get("id")
            chat_id = cb["message"]["chat"]["id"]
            data    = cb.get("data", "")
            self._answer_callback(cb["id"])
            if user_id:
                self._handle_callback(data, user_id, chat_id, frm.get("username"))

    def _handle_command(self, cmd, user_id, chat_id, username, args):
        if cmd in ("/start",):
            self._cmd_start(user_id, chat_id, username)
        elif cmd == "/help":
            self._cmd_help(chat_id)
        elif cmd == "/menu":
            self._cmd_menu(user_id, chat_id, username)
        elif cmd == "/export":
            self._cmd_export(user_id, chat_id)
        elif cmd == "/setdeposit":
            self._cmd_setdeposit(user_id, chat_id, args)
        elif cmd == "/connect":
            self._cmd_connect(user_id, chat_id, username)
        elif cmd == "/cancel":
            self._conv.pop(user_id, None)
            self._send(chat_id, "Отменено.")
        elif cmd == "/disconnect":
            self._cmd_disconnect(user_id, chat_id)
        elif cmd == "/status":
            self._cmd_status(user_id, chat_id)
        elif cmd == "/positions":
            self._cmd_positions(user_id, chat_id)
        elif cmd == "/stats":
            self._cmd_stats(user_id, chat_id)
        elif cmd == "/close":
            self._cmd_close(user_id, chat_id, args)
        elif cmd == "/pause":
            self._cmd_pause(user_id, chat_id, True)
        elif cmd == "/resume":
            self._cmd_pause(user_id, chat_id, False)
        elif cmd in ("/subscription", "/sub"):
            self._cmd_subscription(user_id, chat_id)
        elif cmd == "/pay":
            self._cmd_pay(user_id, chat_id, username)
        # ── админ-команды ──
        elif cmd == "/admin":
            self._admin_overview(user_id, chat_id)
        elif cmd == "/users":
            self._admin_users(user_id, chat_id)
        elif cmd == "/grant":
            self._admin_grant(user_id, chat_id, args)
        elif cmd == "/settrial":
            self._admin_set(user_id, chat_id, 'trial_days', args, "Триал")
        elif cmd == "/setprice":
            self._admin_set(user_id, chat_id, 'sub_price_usd', args, "Цена подписки")
        elif cmd == "/setsubdays":
            self._admin_set(user_id, chat_id, 'sub_days', args, "Длительность подписки")
        elif cmd == "/broadcast":
            self._admin_broadcast(user_id, chat_id, args)
        elif cmd == "/ban":
            self._admin_ban(user_id, chat_id, args, True)
        elif cmd == "/unban":
            self._admin_ban(user_id, chat_id, args, False)

    # Карта действий: и для инлайн-кнопок (act:<action>), и для текста нижнего меню
    def _do_action(self, action, user_id, chat_id, username, arg=None):
        if   action == "status":       self._cmd_status(user_id, chat_id)
        elif action == "positions":    self._cmd_positions(user_id, chat_id)
        elif action == "stats":        self._cmd_stats(user_id, chat_id)
        elif action in ("subscription", "sub"): self._cmd_subscription(user_id, chat_id)
        elif action == "pay":          self._cmd_pay(user_id, chat_id, username)
        elif action == "pause":        self._cmd_pause(user_id, chat_id, True)
        elif action == "resume":       self._cmd_pause(user_id, chat_id, False)
        elif action == "help":         self._cmd_help(chat_id)
        elif action == "connect":      self._cmd_connect(user_id, chat_id, username)
        elif action == "menu":         self._cmd_menu(user_id, chat_id, username)
        elif action == "setdeposit":   self._start_deposit_dialog(user_id, chat_id)
        elif action == "close" and arg: self._cmd_close(user_id, chat_id, [arg])

    # Текст постоянного нижнего меню -> действие (label -> action)
    _MENU = {
        "📊 Статус": "status", "📋 Позиции": "positions", "📈 Статистика": "stats",
        "💳 Подписка": "subscription", "⏸ Пауза": "pause", "▶️ Возобновить": "resume",
        "💰 Депозит": "setdeposit", "❓ Помощь": "help", "🔌 Подключить биржу": "connect",
    }

    def _route_menu_text(self, text, user_id, chat_id, username):
        action = self._MENU.get(text.strip())
        if action:
            self._do_action(action, user_id, chat_id, username)

    def _handle_callback(self, data, user_id, chat_id, username):
        if data.startswith("connect_ex:"):
            exch = data.split(":", 1)[1]
            self._conv[user_id] = {"state": "await_key", "exchange": exch}
            label = EXCHANGE_LABELS.get(exch, exch)
            self._send(chat_id,
                f"Биржа: <b>{label}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Пришли <b>API Key</b> одним сообщением.\n\n"
                f"⚠️ Создай ключ <b>только для торговли</b> (фьючерсы), "
                f"<b>БЕЗ права вывода средств</b>. Сообщение с ключом я удалю автоматически.\n\n"
                f"Отмена: /cancel")
        elif data == "do_connect":
            self._cmd_connect(user_id, chat_id, username)
        elif data.startswith("act:"):
            parts  = data.split(":", 2)   # act:<action>[:arg]
            action = parts[1] if len(parts) > 1 else ""
            arg    = parts[2] if len(parts) > 2 else None
            self._do_action(action, user_id, chat_id, username, arg)

    # ── онбординг ─────────────────────────────────────────────────────────────
    def _cmd_connect(self, user_id, chat_id, username):
        db.upsert_user(user_id, username)
        self._conv[user_id] = {"state": "await_exchange"}
        keyboard = {"inline_keyboard": [
            [{"text": "Bybit", "callback_data": "connect_ex:bybit"}],
            [{"text": "BingX", "callback_data": "connect_ex:bingx"}],
        ]}
        self._send(chat_id,
            "<b>🔌 Подключение биржи</b>\n"
            "Выбери биржу, ключи которой подключаешь:",
            reply_markup=keyboard)

    def _handle_conversation_input(self, user_id, chat_id, message_id, text):
        conv  = self._conv.get(user_id)
        if not conv:
            return
        state = conv.get("state")

        if state == "await_deposit":
            self._conv.pop(user_id, None)
            self._apply_deposit(user_id, chat_id, text)
            return

        if state == "await_key":
            conv["api_key"] = text
            conv["state"]   = "await_secret"
            self._delete_message(chat_id, message_id)   # ключ не должен оставаться в чате
            self._send(chat_id,
                "🔑 API Key принят (сообщение удалено).\n"
                "Теперь пришли <b>API Secret</b>:")

        elif state == "await_secret":
            secret   = text
            api_key  = conv.get("api_key", "")
            exch     = conv.get("exchange", "bybit")
            self._delete_message(chat_id, message_id)   # секрет не должен оставаться в чате
            self._send(chat_id, "⏳ Проверяю ключи на бирже...")

            ok, balance, err = ex.validate_credentials(exch, api_key, secret, config.TRADING_MODE)
            if not ok:
                self._conv.pop(user_id, None)
                self._send(chat_id,
                    f"❌ Ключи не прошли проверку:\n<code>{(err or '')[:300]}</code>\n\n"
                    f"Проверь права ключа и попробуй заново: /connect")
                return

            first_time = self._is_first_connection(user_id)
            db.set_user_keys(user_id, exch, crypto.encrypt(api_key), crypto.encrypt(secret))
            db.start_trial(user_id)          # стартует триал только при первом подключении
            self._conv.pop(user_id, None)    # стираем plaintext-ключи из памяти

            user  = db.get_user(user_id)
            until = db.access_until(user)
            label = EXCHANGE_LABELS.get(exch, exch)
            trial_note = (f"🎁 Бесплатный триал на {db.get_int_setting('trial_days', 7)} дн. активирован!\n"
                          if first_time else "")
            self._send(chat_id,
                f"✅ <b>{label} подключён!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Баланс: <b>${balance:,.2f}</b>\n"
                f"{trial_note}"
                f"Доступ до: <b>{_fmt_until(until)}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Бот начнёт копировать сигналы на твой счёт со следующего цикла.\n"
                f"Риск: {config.RISK_PER_TRADE}% на сделку · до {config.MAX_ACTIVE_PAIRS} позиций.\n\n"
                f"👇 Управляй кнопками меню внизу.\n"
                f"⚠️ Торговля сопряжена с риском убытка. Ты управляешь своими средствами сам.",
                reply_markup=self._main_menu())

    def _is_first_connection(self, user_id) -> bool:
        u = db.get_user(user_id)
        return not (u and (u.get("trial_until") or u.get("paid_until")))

    def _cmd_disconnect(self, user_id, chat_id):
        db.clear_user_keys(user_id)
        if self.platform:
            self.platform.accounts.pop(user_id, None)
            self.platform._fingerprints.pop(user_id, None)
        self._send(chat_id,
            "🔌 Биржа отключена. API-ключи удалены из системы.\n"
            "Открытые позиции (если есть) останутся на твоём счёте — управляй ими на бирже.\n"
            "Подключить снова: /connect")

    # ── команды юзера ─────────────────────────────────────────────────────────
    def _cmd_start(self, user_id, chat_id, username):
        db.upsert_user(user_id, username)
        user = db.get_user(user_id)
        if db.has_keys(user):
            self._send(chat_id, "С возвращением! 👇 Управляй кнопками меню.",
                       reply_markup=self._main_menu())
            self._cmd_status(user_id, chat_id)
            return
        self._send(chat_id,
            "<b>🤖 Fibonacci Trading Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Бот торгует по стратегии SMC + Фибоначчи на фьючерсах и копирует сигналы\n"
            "на ТВОЙ биржевой счёт (твои ключи, твои средства).\n\n"
            f"🎁 Бесплатный триал {db.get_int_setting('trial_days', 7)} дн. при первом подключении.\n"
            f"💵 Подписка: ${db.get_int_setting('sub_price_usd', 30)} / "
            f"{db.get_int_setting('sub_days', 30)} дн.\n\n"
            "Чтобы начать — подключи биржу:",
            reply_markup={"inline_keyboard": [[{"text": "🔌 Подключить биржу", "callback_data": "do_connect"}]]})

    def _cmd_help(self, chat_id):
        self._send(chat_id,
            "<b>🤖 Команды</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Управляй кнопками меню внизу 👇 или командами:\n\n"
            "/connect       — подключить биржу (API-ключи)\n"
            "/disconnect    — отключить биржу\n"
            "/status        — подписка, баланс, позиции\n"
            "/positions     — открытые позиции с PnL\n"
            "/stats         — статистика по твоим сделкам\n"
            "/close BTCUSDT — закрыть позицию вручную\n"
            "/setdeposit N  — депозит для расчёта позиции ($, 0 = весь баланс)\n"
            "/export        — выгрузить журнал сделок файлом (для анализа)\n"
            "/pause /resume — пауза / возобновить\n"
            "/subscription /pay — подписка и оплата\n"
            "/menu          — показать меню кнопок",
            reply_markup=self._main_menu())

    # ── меню на кнопках ────────────────────────────────────────────────────────
    @staticmethod
    def _main_menu():
        """Постоянное нижнее меню (ReplyKeyboard) — всегда под рукой."""
        return {
            "keyboard": [
                [{"text": "📊 Статус"}, {"text": "📋 Позиции"}],
                [{"text": "📈 Статистика"}, {"text": "💳 Подписка"}],
                [{"text": "💰 Депозит"}, {"text": "⏸ Пауза"}],
                [{"text": "❓ Помощь"}],
            ],
            "resize_keyboard": True,
        }

    def _cmd_menu(self, user_id, chat_id, username):
        db.upsert_user(user_id, username)
        self._send(chat_id,
            "<b>📲 Меню</b>\nВыбирай действие кнопками ниже 👇",
            reply_markup=self._main_menu())

    # ── депозит для расчёта позиции (п.3) ──────────────────────────────────────
    def _cmd_setdeposit(self, user_id, chat_id, args):
        if not self._require_connected(user_id, chat_id):
            return
        if not args:
            self._start_deposit_dialog(user_id, chat_id)
            return
        self._apply_deposit(user_id, chat_id, args[0])

    def _start_deposit_dialog(self, user_id, chat_id):
        if not self._require_connected(user_id, chat_id):
            return
        self._conv[user_id] = {"state": "await_deposit"}
        cur = (db.get_user(user_id) or {}).get("deposit_usd")
        cur_s = f"${float(cur):,.0f}" if cur and float(cur) > 0 else "весь баланс"
        self._send(chat_id,
            f"💰 <b>Депозит для расчёта позиции</b>\n"
            f"Текущий: {cur_s}\n\n"
            f"Пришли сумму в USD (например <code>1000</code>). "
            f"Размер сделки = депозит × {config.RISK_PER_TRADE}% риска.\n"
            f"<code>0</code> — считать от всего баланса. Отмена: /cancel")

    def _apply_deposit(self, user_id, chat_id, raw):
        try:
            val = float(str(raw).replace("$", "").replace(",", "").strip())
            if val < 0:
                raise ValueError
        except ValueError:
            self._send(chat_id, "Нужна сумма в USD числом, например 1000 (или 0 = весь баланс).")
            return
        db.set_user_field(user_id, "deposit_usd", val if val > 0 else None)
        if val > 0:
            self._send(chat_id, f"✅ Депозит установлен: <b>${val:,.0f}</b>. "
                                f"Риск {config.RISK_PER_TRADE}% → ${val*config.RISK_PER_TRADE/100:,.2f} на сделку.",
                       reply_markup=self._main_menu())
        else:
            self._send(chat_id, "✅ Сброшено — размер позиции считается от всего баланса.",
                       reply_markup=self._main_menu())

    def _require_connected(self, user_id, chat_id):
        """Возвращает user-dict если ключи подключены, иначе шлёт подсказку и None."""
        user = db.get_user(user_id)
        if not db.has_keys(user):
            self._send(chat_id, "Сначала подключи биржу: /connect")
            return None
        return user

    def _cmd_status(self, user_id, chat_id):
        user = self._require_connected(user_id, chat_id)
        if not user:
            return
        active = db.is_active(user)
        until  = db.access_until(user)
        paused = bool(user.get("paused"))
        exch   = EXCHANGE_LABELS.get(user.get("exchange"), user.get("exchange") or "—")
        dep    = user.get("deposit_usd")

        client    = self._user_client(user_id, user)
        balance   = self._fetch_balance(client)
        positions = self._fetch_open_positions(client)
        pending   = self._fetch_pending_orders(client)
        open_n    = len(positions)
        pairs     = ", ".join(sorted(self._norm(p.get("symbol", "")) for p in positions)) or "—"
        pend_str  = f"  (+{len(pending)} ⏳ ждут)" if pending else ""

        bal_str  = f"${balance:,.2f}" if isinstance(balance, (int, float)) else "—"
        dep_str  = f"${float(dep):,.0f}" if dep and float(dep) > 0 else "весь баланс"
        sub_icon = "✅" if active else "⛔"
        st_icon  = "⏸ Пауза" if paused else ("▶️ Активен" if active else "⛔ Нет доступа")
        days     = _days_left(until)

        markup = {"inline_keyboard": [[
            {"text": "▶️ Возобновить" if paused else "⏸ Пауза",
             "callback_data": "act:resume" if paused else "act:pause"},
            {"text": "🔄 Обновить", "callback_data": "act:status"},
        ]]}
        self._send(chat_id,
            f"<b>📊 Статус</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Биржа:      {exch}\n"
            f"Состояние:  {st_icon}\n"
            f"Подписка:   {sub_icon} до {_fmt_until(until)} ({days} дн.)\n"
            f"Баланс:     <b>{bal_str}</b>\n"
            f"Депозит:    {dep_str}  (расчёт позиции)\n"
            f"Позиций:    {open_n}/{config.MAX_ACTIVE_PAIRS}{pend_str}\n"
            f"Пары:       {pairs}\n"
            + ("" if active else "\n⚠️ Подписка истекла — новые сделки не открываются. /pay"),
            reply_markup=markup)

    def _fetch_pending_orders(self, client):
        """Открытые (не заполненные) лимит-ордера входа с биржи — GTC живёт до 72ч,
        юзер должен видеть, что бот работает, даже когда позиций ещё нет."""
        if client is None:
            return []
        try:
            orders = client.fetch_open_orders(params={'settleCoin': 'USDT'})
            return [o for o in orders if not o.get('reduceOnly')
                    and str(o.get('type', '')).lower() == 'limit']
        except Exception as e:
            log(f"fetch_open_orders error: {e}")
            return []

    def _cmd_positions(self, user_id, chat_id):
        user = self._require_connected(user_id, chat_id)
        if not user:
            return
        client = self._user_client(user_id, user)
        if client is None:
            self._send(chat_id, "Не удалось подключиться к бирже. Попробуй позже.")
            return
        positions = self._fetch_open_positions(client)   # РЕАЛЬНЫЕ позиции с биржи
        pending   = self._fetch_pending_orders(client)   # ожидающие GTC-лимиты входа

        if not positions and not pending:
            self._send(chat_id, "📭 Открытых позиций и ожидающих ордеров нет.")
            return

        lines = ([f"<b>📋 Позиции ({len(positions)})</b>", "━━━━━━━━━━━━━━━━━━━━"]
                 if positions else [])
        buttons = []
        for p in positions:
            pair = self._norm(p.get("symbol", ""))
            side = str(p.get("side", "")).upper()           # LONG / SHORT
            entry = float(p.get("entryPrice") or 0)
            mark  = float(p.get("markPrice") or 0)
            upnl  = p.get("unrealizedPnl")
            info  = p.get("info", {}) or {}
            sl    = info.get("stopLoss")  or "—"
            tp    = info.get("takeProfit") or "—"
            dir_icon = "📈" if side == "LONG" else "📉"
            pnl_str = ""
            if upnl is not None:
                try:
                    u = float(upnl); ic = "🟢" if u >= 0 else "🔴"
                    pnl_str = f"  {ic} <b>${u:+.2f}</b>"
                except Exception:
                    pass
            sl_s = f"${_fmt_p(float(sl))}" if str(sl) not in ("—", "", "0") else "—"
            tp_s = f"${_fmt_p(float(tp))}" if str(tp) not in ("—", "", "0") else "—"
            lines.append(
                f"\n{dir_icon} <b>{pair}</b> {side}{pnl_str}\n"
                f"   Вход <code>${_fmt_p(entry)}</code> @ <code>${_fmt_p(mark)}</code>\n"
                f"   🛡 SL <code>{sl_s}</code> · 🎯 TP <code>{tp_s}</code>")
            buttons.append([{"text": f"❌ Закрыть {pair}", "callback_data": f"act:close:{pair}"}])

        if pending:
            if lines:
                lines.append("")
            lines.append(f"<b>⏳ Ожидают заполнения ({len(pending)})</b>")
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            for o in pending:
                pair = self._norm(o.get("symbol", ""))
                side = "LONG" if str(o.get("side", "")).lower() == "buy" else "SHORT"
                price = float(o.get("price") or 0)
                lines.append(f"⏳ <b>{pair}</b> {side} — лимит <code>${_fmt_p(price)}</code>")
            lines.append("<i>GTC-лимит ждёт касания цены (до "
                         f"{config.PENDING_ORDER_MAX_HOURS:.0f}ч), отменится сам при "
                         f"инвалидации сетапа.</i>")

        self._send(chat_id, "\n".join(lines),
                   reply_markup={"inline_keyboard": buttons} if buttons else None)

    def _cmd_stats(self, user_id, chat_id):
        user = self._require_connected(user_id, chat_id)
        if not user:
            return
        stats = compute_stats(db.get_user_trades(user_id))
        if not stats:
            self._send(chat_id, "📊 Статистика пуста — закрытых сделок ещё не было.")
            return
        pf = stats["profit_factor"]
        pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
        avg_h, avg_m = int(stats["avg_duration"] // 60), int(stats["avg_duration"] % 60)
        dur = f"{avg_h}ч {avg_m}м" if avg_h > 0 else f"{avg_m}м"
        l_cnt, l_w = stats["longs"]; s_cnt, s_w = stats["shorts"]
        self._send(chat_id,
            f"<b>📊 Твоя статистика</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Сделок:        <b>{stats['total']}</b>\n"
            f"Прибыльных:    <b>{stats['wins']}</b> ({stats['win_rate']:.1f}%)\n"
            f"Убыточных:     <b>{stats['losses']}</b>\n"
            f"Profit Factor: <b>{pf_str}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Total PnL:  <b>${stats['total_pnl']:+.2f}</b>\n"
            f"Сегодня:    <b>${stats['today_pnl']:+.2f}</b>\n"
            f"Ср. сделка: {dur}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 Лучшая:  <code>${stats['best'][0]:+.2f}</code> {stats['best'][1]}\n"
            f"📉 Худшая:  <code>${stats['worst'][0]:+.2f}</code> {stats['worst'][1]}\n"
            f"📈 LONG: {l_cnt} · 📉 SHORT: {s_cnt}")

    def _cmd_close(self, user_id, chat_id, args):
        user = self._require_connected(user_id, chat_id)
        if not user:
            return
        client = self._user_client(user_id, user)
        if client is None:
            self._send(chat_id, "Не удалось подключиться к бирже. Попробуй позже.")
            return
        if not args:
            positions = self._fetch_open_positions(client)
            if positions:
                buttons = [[{"text": f"❌ Закрыть {self._norm(p.get('symbol',''))}",
                             "callback_data": f"act:close:{self._norm(p.get('symbol',''))}"}]
                           for p in positions]
                self._send(chat_id, "Выбери позицию для закрытия:",
                           reply_markup={"inline_keyboard": buttons})
            else:
                self._send(chat_id, "📭 Открытых позиций нет.")
            return
        pair = args[0].upper()
        if not pair.endswith("USDT"):
            pair += "USDT"
        self._send(chat_id, f"⏳ Закрываю <b>{pair}</b>...")
        # Через живой менеджер (журнал/уведомления) или напрямую с биржи (untracked)
        mgr = self._live_manager(user_id)
        if mgr is not None:
            success, price = mgr.close_position_by_pair(pair)
        else:
            success, price = self._raw_close(client, pair)
        if success:
            self._send(chat_id, f"✅ <b>{pair}</b> закрыта @ <code>${_fmt_p(price)}</code>")
        else:
            self._send(chat_id, f"⚠️ <b>{pair}</b> не найдена или не удалось закрыть.")

    def _raw_close(self, client, pair):
        """Закрытие позиции напрямую через клиент (нет live-менеджера / untracked)."""
        try:
            target = None
            for p in client.fetch_positions():
                if self._norm(p.get("symbol", "")) == self._norm(pair) and \
                   abs(float(p.get("contracts", 0) or 0)) > 0:
                    target = p
                    break
            if not target:
                return False, 0.0
            size = abs(float(target.get("contracts", 0) or 0))
            is_long = str(target.get("side", "")).lower() == "long"
            side = "sell" if is_long else "buy"
            try:
                client.cancel_all_orders(pair)
            except Exception:
                pass
            client.create_order(symbol=pair, type="market", side=side, amount=size,
                                params={"reduce_only": True})
            price = client.fetch_ticker(pair)["last"]
            return True, price
        except Exception as e:
            log(f"raw close {pair}: {e}")
            return False, 0.0

    def _cmd_pause(self, user_id, chat_id, pause: bool):
        user = self._require_connected(user_id, chat_id)
        if not user:
            return
        db.set_user_field(user_id, "paused", 1 if pause else 0)
        if pause:
            self._send(chat_id, "⏸ Новые входы приостановлены. Открытые позиции продолжают вестись.")
        else:
            self._send(chat_id, "▶️ Торговля возобновлена.")

    def _cmd_subscription(self, user_id, chat_id):
        user = db.get_user(user_id)
        if not user:
            db.upsert_user(user_id)
            user = db.get_user(user_id)
        active = db.is_active(user)
        until  = db.access_until(user)
        days   = _days_left(until)
        price  = db.get_int_setting("sub_price_usd", 30)
        sdays  = db.get_int_setting("sub_days", 30)
        head   = "✅ Подписка активна" if active else "⛔ Подписка неактивна"
        self._send(chat_id,
            f"<b>{head}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Доступ до: <b>{_fmt_until(until)}</b> ({days} дн.)\n"
            f"Тариф: <b>${price}</b> / {sdays} дн.",
            reply_markup={"inline_keyboard": [[
                {"text": f"💳 Оплатить ${price}", "callback_data": "act:pay"}]]})

    def _cmd_pay(self, user_id, chat_id, username):
        import payments
        db.upsert_user(user_id, username)
        try:
            inv = payments.create_invoice(user_id)
        except Exception as e:
            log(f"create_invoice error {user_id}: {e}")
            self._send(chat_id, "⚠️ Не удалось создать счёт. Попробуй позже или напиши в поддержку.")
            return
        price = inv['amount']
        sdays = db.get_int_setting('sub_days', 30)
        url   = inv.get('pay_url')
        markup = None
        if url:
            markup = {"inline_keyboard": [[{"text": f"💳 Оплатить ${price}", "url": url}]]}
        self._send(chat_id,
            f"<b>💵 Оплата подписки</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Сумма: <b>${price}</b> за {sdays} дн.\n"
            f"Актив: {inv['currency'].upper()}\n"
            + (f"Ссылка для оплаты ниже 👇\n" if url else f"Счёт создан: <code>{inv['invoice_id']}</code>\n")
            + f"\nПосле подтверждения сети подписка продлится автоматически "
              f"(обычно в течение нескольких минут).",
            reply_markup=markup)

    # ── админ-команды ─────────────────────────────────────────────────────────
    def _is_admin(self, user_id) -> bool:
        return user_id in config.ADMIN_IDS

    def _resolve_target(self, token: str):
        """Находит telegram_id по '12345' или '@username'. None если не найден."""
        token = token.strip()
        if token.lstrip('-').isdigit():
            return int(token)
        if token.startswith('@'):
            uname = token[1:].lower()
            for u in db.list_users():
                if (u.get('username') or '').lower() == uname:
                    return u['telegram_id']
        return None

    def _admin_overview(self, user_id, chat_id):
        if not self._is_admin(user_id):
            return
        users    = db.list_users()
        with_keys = [u for u in users if db.has_keys(u)]
        active    = [u for u in users if db.is_active(u) and db.has_keys(u)]
        banned    = [u for u in users if u.get('status') == 'banned']
        live      = len(self.platform.accounts) if self.platform else 0
        self._send(chat_id,
            f"<b>🛠 Админ-панель</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Всего юзеров:    <b>{len(users)}</b>\n"
            f"С ключами:       <b>{len(with_keys)}</b>\n"
            f"Активных:        <b>{len(active)}</b>\n"
            f"В реестре (live):<b> {live}</b>\n"
            f"Забанено:        <b>{len(banned)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Триал:    {db.get_int_setting('trial_days', 7)} дн.\n"
            f"Подписка: ${db.get_int_setting('sub_price_usd', 30)} / "
            f"{db.get_int_setting('sub_days', 30)} дн.\n"
            f"Напоминание: за {db.get_int_setting('expiry_reminder_days', 3)} дн.\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"/users · /grant ID ДНЕЙ · /settrial N · /setprice N\n"
            f"/setsubdays N · /broadcast ТЕКСТ · /ban ID · /unban ID")

    def _admin_users(self, user_id, chat_id):
        if not self._is_admin(user_id):
            return
        users = db.list_users()
        if not users:
            self._send(chat_id, "Юзеров нет.")
            return
        lines = ["<b>👥 Пользователи</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for u in users[:50]:
            tid    = u['telegram_id']
            uname  = f"@{u['username']}" if u.get('username') else str(tid)
            active = "✅" if db.is_active(u) else "⛔"
            keys   = "🔑" if db.has_keys(u) else "—"
            ban    = " 🚫" if u.get('status') == 'banned' else ""
            until  = _fmt_until(db.access_until(u))
            lines.append(f"{active}{keys} <code>{tid}</code> {uname} · до {until}{ban}")
        if len(users) > 50:
            lines.append(f"… и ещё {len(users) - 50}")
        self._send(chat_id, "\n".join(lines))

    def _admin_grant(self, user_id, chat_id, args):
        if not self._is_admin(user_id):
            return
        if len(args) < 2:
            self._send(chat_id, "Использование: /grant ID|@user ДНЕЙ")
            return
        target = self._resolve_target(args[0])
        if target is None:
            self._send(chat_id, f"Юзер <code>{args[0]}</code> не найден.")
            return
        try:
            days = int(args[1])
        except ValueError:
            self._send(chat_id, "Дней должно быть целым числом.")
            return
        db.upsert_user(target)
        db.extend_subscription(target, days)
        u = db.get_user(target)
        until = db.access_until(u)
        self._send(chat_id, f"✅ Юзеру <code>{target}</code> добавлено {days} дн. Доступ до {_fmt_until(until)}.")
        tg.subscription_extended(days, _fmt_until(until), telegram_id=target)

    def _admin_set(self, user_id, chat_id, key, args, label):
        if not self._is_admin(user_id):
            return
        if not args or not args[0].lstrip('-').isdigit():
            self._send(chat_id, f"Использование: число. Текущее {label}: {db.get_int_setting(key)}")
            return
        db.set_setting(key, int(args[0]))
        self._send(chat_id, f"✅ {label} = {args[0]}")

    def _admin_broadcast(self, user_id, chat_id, args):
        if not self._is_admin(user_id):
            return
        if not args:
            self._send(chat_id, "Использование: /broadcast ТЕКСТ")
            return
        text = "📢 " + " ".join(args)
        sent = 0
        for u in db.list_users():
            if u.get('status') == 'banned':
                continue
            self._send(u['telegram_id'], text)
            sent += 1
        self._send(chat_id, f"✅ Разослано {sent} юзерам.")

    def _admin_ban(self, user_id, chat_id, args, ban: bool):
        if not self._is_admin(user_id):
            return
        if not args:
            self._send(chat_id, f"Использование: /{'ban' if ban else 'unban'} ID|@user")
            return
        target = self._resolve_target(args[0])
        if target is None:
            self._send(chat_id, f"Юзер <code>{args[0]}</code> не найден.")
            return
        db.set_user_field(target, 'status', 'banned' if ban else 'active')
        if ban and self.platform:
            self.platform.accounts.pop(target, None)
            self.platform._fingerprints.pop(target, None)
        self._send(chat_id, f"{'🚫 Забанен' if ban else '✅ Разбанен'}: <code>{target}</code>")

    # ── helpers: доступ к бирже (источник истины для статуса/позиций) ─────────
    @staticmethod
    def _norm(symbol: str) -> str:
        """'BTC/USDT:USDT' и 'BTCUSDT' -> 'BTCUSDT'."""
        return (symbol or "").replace("/", "").replace(":USDT", "").replace(":", "").upper()

    def _live_manager(self, user_id):
        if self.platform:
            acc = self.platform.get_account(user_id)
            if acc and acc.ok:
                return acc.manager
        return None

    def _user_client(self, user_id, user):
        """ccxt-клиент юзера: из живого менеджера, иначе временный (для запросов к бирже)."""
        mgr = self._live_manager(user_id)
        if mgr is not None:
            return mgr.exchange
        try:
            key = crypto.decrypt(user.get("api_key_enc") or "")
            sec = crypto.decrypt(user.get("api_secret_enc") or "")
            if not key or not sec:
                return None
            return ex.make_client(user.get("exchange") or "bybit", key, sec, config.TRADING_MODE)
        except Exception as e:
            log(f"client build error {user.get('telegram_id')}: {e}")
            return None

    def _fetch_balance(self, client):
        if client is None:
            return None
        try:
            bal = client.fetch_balance()
            return bal.get("USDT", {}).get("total", 0) or 0
        except Exception as e:
            log(f"balance fetch error: {e}")
            return None

    def _fetch_open_positions(self, client):
        """Реальные открытые позиции с биржи (contracts>0) — источник истины."""
        if client is None:
            return []
        out = []
        try:
            for p in client.fetch_positions():
                if abs(float(p.get("contracts", 0) or 0)) > 0:
                    out.append(p)
        except Exception as e:
            log(f"fetch_positions error: {e}")
        return out

    def _cmd_export(self, user_id, chat_id):
        """Шлёт юзеру ЕГО файл детального журнала сделок (state/<id>/trades_detail.jsonl)."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'state', str(user_id), 'trades_detail.jsonl')
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            self._send(chat_id, "📭 Журнал пока пуст — файл появится после первой закрытой сделки.")
            return
        if self._send_document(chat_id, path):
            self._send(chat_id, "📎 Детальный журнал сделок выгружен (JSONL, 62 поля на сделку).")
        else:
            self._send(chat_id, "⚠️ Не удалось отправить файл. Попробуй позже.")

    # ── low-level HTTP ────────────────────────────────────────────────────────
    def _send_document(self, chat_id, path) -> bool:
        if not config.TELEGRAM_BOT_TOKEN:
            return False
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendDocument"
            with open(path, 'rb') as f:
                r = requests.post(url, data={"chat_id": chat_id},
                                  files={"document": f}, timeout=60)
            return bool(r.json().get('ok'))
        except Exception as e:
            log(f"Platform sendDocument error: {e}")
            return False

    def _send(self, chat_id, text, reply_markup=None):
        if not config.TELEGRAM_BOT_TOKEN:
            return
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            log(f"Platform TG send error: {e}")

    def _delete_message(self, chat_id, message_id):
        if not config.TELEGRAM_BOT_TOKEN or not message_id:
            return
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/deleteMessage"
            requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
        except Exception:
            pass

    def _answer_callback(self, callback_id):
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
            requests.post(url, json={"callback_query_id": callback_id}, timeout=5)
        except Exception:
            pass

    def _set_commands(self):
        if not config.TELEGRAM_BOT_TOKEN:
            return
        commands = [
            {"command": "start",        "description": "Начало / подключение"},
            {"command": "connect",      "description": "Подключить биржу (API-ключи)"},
            {"command": "status",       "description": "Подписка, баланс, позиции"},
            {"command": "positions",    "description": "Открытые позиции с PnL"},
            {"command": "stats",        "description": "Статистика по сделкам"},
            {"command": "close",        "description": "Закрыть позицию: /close BTCUSDT"},
            {"command": "pause",        "description": "Приостановить новые входы"},
            {"command": "resume",       "description": "Возобновить торговлю"},
            {"command": "subscription", "description": "Статус подписки"},
            {"command": "pay",          "description": "Оплатить / продлить подписку"},
            {"command": "setdeposit",   "description": "Депозит для расчёта позиции ($)"},
            {"command": "menu",         "description": "Показать меню кнопок"},
            {"command": "export",       "description": "Выгрузить журнал сделок (файл)"},
            {"command": "disconnect",   "description": "Отключить биржу"},
            {"command": "help",         "description": "Список команд"},
        ]
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/setMyCommands"
            requests.post(url, json={"commands": commands}, timeout=10)
        except Exception as e:
            log(f"Platform setMyCommands error: {e}")


# Singleton — импортируется platform_bot.py
controller = PlatformController()
