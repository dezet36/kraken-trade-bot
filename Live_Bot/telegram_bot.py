"""
Telegram-панель управления ботом: одно меню, которое перерисовывается на месте.

ПОЧЕМУ ПЕРЕПИСАНА (26.09.2026). Панель писалась под боевой счёт и на
бумажном работала через переходник брокера — с поломками, которые видел
владелец:
  • «Позиции» падали (время входа с поясом минус время без пояса) — при
    открытых позициях кнопка не отвечала вовсе;
  • «Статус» подписывал бумажный счёт «🔴 LIVE», писал «Позиций 16/5» и
    «сегодня 0 сделок» — считал только сделки с последнего перезапуска;
  • «Статистика» смешивала пять стратегий и показывала всем зоны фибо;
  • «Выгрузка» искала файлы боевого журнала и отвечала «журнал пуст»;
  • каждая кнопка слала НОВОЕ сообщение — чат зарастал копиями меню;
  • пауза и «без звука» жили в памяти и снимались каждой выкаткой;
  • закрытие позиции — без подтверждения.

КАК ТЕПЕРЬ. Экраны рисует telegram_panel (чистые функции), здесь — сбор
данных, маршрут кнопок, действия и HTTP. Кнопка меняет то сообщение, на
котором нажата (editMessageText); кнопка из уведомления («!» в коде)
открывает панель новым сообщением, не затирая уведомление.

Действия, меняющие позиции, — только через подтверждение со сроком
годности (CONFIRM_TTL_S): нажатие, пролежавшее в очереди Telegram, пока бот
перезапускался, не закроет позицию через полчаса. Увеличить риск отсюда
нельзя — только закрыть, снять, стоп в безубыток, пауза.

Боевой счёт (LiveTradeManager) панель не знает — у него нет snapshot();
для него оставлены прежние экраны (_legacy_*).
"""

import json
import os
import threading
import time
from datetime import datetime, timezone

import requests

import config
import telegram_state
from logger import log

CONFIRM_TTL_S = 600
POLL_TIMEOUT_S = 25

# Команды меню Telegram и экран, который открывает каждая (None — своя ветка).
COMMANDS = (
    ('menu', 'Панель: капитал, сегодня, стратегии', 'm'),
    ('positions', 'Позиции: карточка, безубыток, закрыть', 'pl:0'),
    ('orders', 'Заявки: карточка, снять', 'ol:0'),
    ('setups', 'Сетапы ИИ: ждут условия, цену, в позиции', 'ai'),
    ('stats', 'Статистика по стратегиям', 'st:7'),
    ('strategies', 'Стратегии: входы вкл/выкл', 'sl'),
    ('notify', 'Какие уведомления присылать', 'nt'),
    ('pause', 'Остановить новые входы', None),
    ('resume', 'Возобновить новые входы', None),
    ('mute', 'Выключить все уведомления', None),
    ('unmute', 'Включить уведомления', None),
    ('export', 'Журнал сделок файлами', None),
    ('help', 'Как пользоваться', 'h'),
)
ALIASES = {'start': 'm', 'status': 'm'}


def _stamp(now=None):
    """Метка подтверждения: секунды в base36 — коротко для callback_data (≤ 64 байт)."""
    n = int(now if now is not None else time.time())
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while n:
        n, k = divmod(n, 36)
        out = digits[k] + out
    return out or '0'


def _fresh(stamp, now=None):
    try:
        made = int(stamp, 36)
    except (TypeError, ValueError):
        return False
    age = (now if now is not None else time.time()) - made
    return 0 <= age <= CONFIRM_TTL_S


def _int(parts, default=0):
    try:
        return int(parts[0])
    except (IndexError, TypeError, ValueError):
        return default


class BotController:
    def __init__(self):
        self._lock = threading.Lock()
        self._offset = 0
        self._running = False
        self._thread = None
        self._conflict_logged = False
        self.trade_manager = None  # брокер бумажного счёта или LiveTradeManager — ставит bot.py
        # Пауза и «без звука» переживают перезапуск (telegram_state).
        state = telegram_state.load()
        self._paused = bool(state.get('paused', False))
        self._muted = bool(state.get('muted', False))

    # ── Публичное ────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg-panel")
        self._thread.start()
        self._set_commands()
        log("Telegram-панель запущена (polling)"
            + (" — новые входы НА ПАУЗЕ с прошлого запуска, снять: /resume" if self._paused else ""))

    def stop(self):
        self._running = False

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def is_muted(self) -> bool:
        with self._lock:
            return self._muted

    def set_paused(self, value, source='Telegram'):
        with self._lock:
            self._paused = bool(value)
        telegram_state.update(paused=bool(value))
        log(f"🖐 Новые входы {'на паузе' if value else 'возобновлены'} ({source})")

    def set_muted(self, value, source='Telegram'):
        with self._lock:
            self._muted = bool(value)
        telegram_state.update(muted=bool(value))
        log(f"🖐 Уведомления Telegram {'выключены' if value else 'включены'} ({source})")

    # ── Опрос ────────────────────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                self._fetch_updates()
            except Exception as e:                     # noqa: BLE001
                log(f"Telegram polling error: {self._safe(e)}")
                time.sleep(5)

    def _fetch_updates(self):
        token = config.TELEGRAM_BOT_TOKEN
        if not token:
            time.sleep(30)
            return
        # Длинный опрос: запрос висит до POLL_TIMEOUT_S и возвращается сразу,
        # как только нажата кнопка. Прежний короткий (5 с + пауза 1 с) слал
        # запрос каждые шесть секунд и отвечал на нажатие с задержкой.
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": self._offset, "timeout": POLL_TIMEOUT_S,
                        "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=POLL_TIMEOUT_S + 10)
        except Exception:                              # noqa: BLE001
            time.sleep(3)
            return
        if not resp.ok:
            # 409 — тот же токен опрашивает ещё один процесс: две копии бота.
            if resp.status_code == 409 and not self._conflict_logged:
                self._conflict_logged = True
                log("⚠️ Telegram: токен опрашивает ещё один процесс (409) — кнопки будут "
                    "отвечать через раз. Один токен — один опрос.")
            time.sleep(5)
            return
        for update in resp.json().get("result", []):
            self._offset = update["update_id"] + 1
            try:
                self._handle_update(update)
            except Exception as e:                     # noqa: BLE001
                log(f"Telegram update handler error: {self._safe(e)}")

    # ── Маршрут ──────────────────────────────────────────────────────────────

    @staticmethod
    def _authorized(chat_id):
        return bool(config.TELEGRAM_CHAT_ID) and str(chat_id) == str(config.TELEGRAM_CHAT_ID)

    def _handle_update(self, update):
        if "message" in update:
            msg = update["message"]
            chat_id = str(msg["chat"]["id"])
            if not self._authorized(chat_id):
                return
            text = msg.get("text", "").strip()
            if text.startswith("/"):
                parts = text.split()
                cmd = parts[0].lower().split("@")[0]
                self._handle_command(cmd, chat_id, parts[1:])
        elif "callback_query" in update:
            cb = update["callback_query"]
            msg = cb.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if not self._authorized(chat_id):
                return
            self._handle_callback(cb.get("data") or "", chat_id, msg.get("message_id"), cb["id"])

    def _handle_command(self, cmd: str, chat_id: str, args: list = None):
        args = args or []
        name = cmd.lstrip('/').lower()
        if name in ('pause', 'resume'):
            self.set_paused(name == 'pause')
            name = 'menu'
        if name in ('mute', 'unmute'):
            self.set_muted(name == 'mute')
            self._send(chat_id, "🔇 <b>Уведомления выключены</b> — кнопки и команды работают. "
                                "Включить: /unmute" if name == 'mute' else "🔔 <b>Уведомления включены</b>")
            return
        if name == 'export':
            self._send_export(chat_id)
            return
        if name == 'close':
            if not self._paper():
                self._legacy_close_command(args, chat_id)
                return
            code = self._close_code(args)
        else:
            code = ALIASES.get(name) or next((c for n, _d, c in COMMANDS if n == name and c), None)
        if code is None:
            code = 'h'
        toast, text, keyboard = self._render(code)
        if text:
            self._send(chat_id, text, reply_markup=keyboard)

    def _close_code(self, args):
        """/close DOGE → подтверждение, если позиция одна; иначе список позиций."""
        if not args:
            return 'pl:0'
        pair = args[0].upper()
        if not pair.endswith('USDT'):
            pair += 'USDT'
        try:
            found = [p for p in self.trade_manager.snapshot().get('open') or [] if p.get('pair') == pair]
        except Exception:                              # noqa: BLE001
            found = []
        if len(found) == 1:
            return f"pc:{found[0]['strategy']}:{pair}"
        return 'pl:0'

    def _handle_callback(self, data, chat_id, message_id, callback_id):
        fresh_message = data.startswith('!')
        code = data.lstrip('!')
        try:
            toast, text, keyboard = self._render(code)
        except Exception as e:                         # noqa: BLE001
            log(f"Telegram: экран «{code}» не собран — {self._safe(e)}")
            toast, text, keyboard = 'Не получилось — подробности в журнале бота', None, None
        self._answer_callback(callback_id, toast)
        if not text:
            return
        if fresh_message or not message_id:
            self._send(chat_id, text, reply_markup=keyboard)
        else:
            self._edit(chat_id, message_id, text, keyboard)

    # ── Экраны ───────────────────────────────────────────────────────────────

    def _paper(self):
        return hasattr(self.trade_manager, 'snapshot')

    def _render(self, code):
        """Код кнопки → (всплывающая подсказка, текст, кнопки). Действия — здесь же."""
        import telegram_panel as panel
        if not self._paper():
            return self._legacy_render(code)
        parts = code.split(':')
        head, rest = parts[0], parts[1:]

        if head == 'y':
            return self._confirmed(rest)
        if head == 'pz':
            self.set_paused(not self.is_paused())
            return ('Новые входы на паузе' if self.is_paused() else 'Пауза снята',
                    *panel.main_view(self._collect()))
        if head == 'nm':
            self.set_muted(not self.is_muted())
            return ('Уведомления выключены' if self.is_muted() else 'Уведомления включены',
                    *panel.notify_view(self._collect()))
        if head == 'ne' and rest:
            return self._toggle_event(rest[0])
        if head in ('ns', 'sn') and rest:
            return self._toggle_strategy_notify(rest[0], back_to_strategy=(head == 'sn'))
        if head == 'se' and rest:
            return self._toggle_strategy(rest[0])

        d = self._collect()
        views = {
            'm': lambda: panel.main_view(d),
            'pl': lambda: panel.positions_view(d, _int(rest)),
            'p': lambda: panel.position_view(d, rest[0], rest[1]),
            'pc': lambda: panel.close_confirm_view(d, rest[0], rest[1], _stamp()),
            'pb': lambda: panel.breakeven_confirm_view(d, rest[0], rest[1], _stamp()),
            'ol': lambda: panel.orders_view(d, _int(rest)),
            'o': lambda: panel.order_view(d, rest[0], rest[1]),
            'oc': lambda: panel.cancel_confirm_view(d, rest[0], rest[1], _stamp()),
            'ai': lambda: panel.ai_view(d),
            'st': lambda: panel.stats_view(d, rest[0] if rest else '7'),
            'sl': lambda: panel.strategies_view(d),
            's': lambda: panel.strategy_view(d, rest[0]),
            'sa': lambda: panel.close_all_confirm_view(d, rest[0], _stamp()),
            'nt': lambda: panel.notify_view(d),
            'h': panel.help_view,
        }
        try:
            text, keyboard = views.get(head, views['m'])()
        except IndexError:                             # код без обязательной части
            text, keyboard = panel.main_view(d)
        return '', text, keyboard

    def _collect(self):
        """Всё, что рисует панель, — одним снимком. Отказ части — прочерк, не падение."""
        tm = self.trade_manager
        d = {'mode': config.TRADING_MODE, 'now': datetime.now(timezone.utc),
             'paused': self.is_paused(), 'muted': self.is_muted(), 'model': None,
             'strategies': {}, 'open': [], 'pending': [], 'settings': {}, 'trades': [],
             'ai': {'armed': [], 'pending': [], 'open': []}, 'cycle_min': self._cycle_age()}
        try:
            snap = tm.snapshot() or {}
            d['strategies'] = snap.get('strategies') or {}
            d['open'] = snap.get('open') or []
            d['pending'] = snap.get('pending') or []
        except Exception as e:                         # noqa: BLE001
            log(f"Telegram: состояние брокера не прочитано — {e}")
        try:
            import settings_store
            d['settings'] = settings_store.load()
        except Exception:                              # noqa: BLE001
            pass
        try:
            from dashboard import _read_paper_trades
            d['trades'] = _read_paper_trades()
        except Exception:                              # noqa: BLE001
            pass
        try:
            import strategy_llm
            d['ai'] = strategy_llm.current_setups(tm)
            d['model'] = strategy_llm.busy()
        except Exception:                              # noqa: BLE001
            pass
        return d

    @staticmethod
    def _cycle_age():
        """Сколько минут назад прошёл цикл бота (bot.note_cycle пишет метку)."""
        try:
            with open(os.path.join(config.DATA_DIR, 'last_cycle.json'), encoding='utf-8') as fh:
                ts = float(json.load(fh).get('ts') or 0)
            return max(0.0, (time.time() - ts) / 60) if ts else None
        except (OSError, ValueError):
            return None

    # ── Переключатели ────────────────────────────────────────────────────────

    def _toggle_event(self, event):
        import settings_store
        import telegram_panel as panel
        if event in {e for e, _l, _w in panel.EVENTS}:
            on = settings_store.notify_on(event, 'telegram')
            settings_store.save({settings_store.NOTIFY: {f'{event}_telegram': not on}})
            log(f"🖐 Уведомление «{event}» в Telegram {'выключено' if on else 'включено'} (Telegram)")
            toast = 'Выключено' if on else 'Включено'
        else:
            toast = ''
        return (toast, *panel.notify_view(self._collect()))

    def _toggle_strategy_notify(self, name, back_to_strategy=False):
        import settings_store
        import telegram_panel as panel
        on = settings_store.notify_strategy(name)
        settings_store.save({name: {'notify': not on}})
        log(f"🖐 Сделки {name} в Telegram {'не присылать' if on else 'присылать'} (Telegram)")
        d = self._collect()
        view = panel.strategy_view(d, name) if back_to_strategy else panel.notify_view(d)
        return ('Сделки этой стратегии не присылаются' if on else 'Сделки этой стратегии присылаются', *view)

    def _toggle_strategy(self, name):
        """Выключить входы — через подтверждение; включить — сразу (это снятие ограничения)."""
        import settings_store
        import telegram_panel as panel
        d = self._collect()
        if settings_store.enabled(name):
            return ('', *panel.strategy_off_confirm_view(d, name, _stamp()))
        settings_store.save({name: {'enabled': True}})
        log(f"🖐 {name}: входы включены (Telegram)")
        return ('Входы включены', *panel.strategy_view(self._collect(), name))

    # ── Действия после подтверждения ─────────────────────────────────────────

    def _confirmed(self, rest):
        import settings_store
        import telegram_panel as panel
        if len(rest) < 3:
            return ('', *panel.main_view(self._collect()))
        action, stamp, args = rest[0], rest[1], rest[2:]
        if not _fresh(stamp):
            return ('Подтверждение устарело', *panel.result_view(
                False, 'Подтверждение устарело — прошло больше 10 минут. Откройте карточку заново.',
                ('◀ Назад', 'm')))
        tm = self.trade_manager
        strategy = args[0]
        pair = args[1] if len(args) > 1 else ''
        if action == 'c':
            ok, msg = tm.close_one(strategy, pair)
            if ok:
                msg = self._closed_text(strategy, pair) or msg
            back = ('◀ Позиции', 'pl:0')
        elif action == 'b':
            ok, msg = tm.move_to_breakeven(strategy, pair)
            back = ('◀ Позиция', f'p:{strategy}:{pair}')
        elif action == 'x':
            ok, msg = tm.cancel_pending(strategy, pair)
            back = ('◀ Заявки', 'ol:0')
        elif action == 'a':
            ok, msg = tm.close_all(strategy)
            back = ('◀ Стратегия', f's:{strategy}')
        elif action == 'f':
            settings_store.save({strategy: {'enabled': False}})
            ok, msg = True, (f'{strategy}: новые входы выключены. Позиции и заявки ведутся как '
                             f'обычно; включить — кнопкой на карточке стратегии.')
            back = ('◀ Стратегия', f's:{strategy}')
        else:
            return ('', *panel.main_view(self._collect()))
        log(f"🖐 Telegram: {action} {strategy} {pair} — {'готово' if ok else 'не вышло'}: {msg}")
        return ('Готово' if ok else 'Не вышло', *panel.result_view(ok, msg, back))

    @staticmethod
    def _closed_text(strategy, pair):
        """Итог только что закрытой вручную позиции — из журнала сделок."""
        try:
            import paper_broker
            import tg_format as fmt
            rows = [r for r in paper_broker.read_journal()
                    if r.get('strategy') == strategy and r.get('pair') == pair]
            if not rows:
                return ''
            r = rows[-1]
            return (f"{pair} {r.get('direction', '')} · {fmt.name(strategy)} закрыта по "
                    f"{fmt.price(r.get('exit_price'))}: {fmt.r(r.get('pnl_r'))} ({fmt.money(r.get('pnl_usd'))})")
        except Exception:                              # noqa: BLE001
            return ''

    # ── Выгрузка ─────────────────────────────────────────────────────────────

    def _send_export(self, chat_id: str):
        """
        Журнал сделок файлами. На бумажном счёте — paper_trades.*: до 26.09.2026
        выгрузка искала файлы БОЕВОГО журнала и отвечала «журнал пуст» при
        семидесяти семи сделках.
        """
        if self._paper():
            import paper_broker
            files = [paper_broker.JOURNAL_CSV, paper_broker.JOURNAL_JSON]
        else:
            files = [os.path.join(config.DATA_DIR, 'trades_detail.jsonl'),
                     os.path.join(config.DATA_DIR, 'trades_journal.csv')]
        sent = 0
        for path in files:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                if self._send_document(chat_id, path):
                    sent += 1
        if sent == 0:
            self._send(chat_id, "📭 Журнал пока пуст — файлы появятся после первой закрытой сделки.")
        else:
            self._send(chat_id, f"📎 Выгружено файлов: {sent} — журнал сделок (CSV для Excel и полный JSONL).")

    # ── HTTP ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _safe(error):
        """Текст ошибки без токена: адрес Bot API несёт его открытым текстом."""
        text = str(error)
        token = config.TELEGRAM_BOT_TOKEN or ''
        return text.replace(token, '<TOKEN>') if token else text

    def _api(self, method, payload, timeout=15):
        token = config.TELEGRAM_BOT_TOKEN
        if not token:
            return None
        try:
            resp = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                                 json=payload, timeout=timeout)
            body = resp.json()
        except Exception as e:                         # noqa: BLE001
            log(f"Telegram {method}: {self._safe(e)}")
            return None
        if not body.get('ok'):
            desc = str(body.get('description', ''))
            if 'message is not modified' not in desc:
                log(f"Telegram {method}: {resp.status_code} {desc[:160]}")
        return body

    def _send(self, chat_id: str, text: str, reply_markup: dict = None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._api("sendMessage", payload)

    def _edit(self, chat_id, message_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        body = self._api("editMessageText", payload)
        if body is None or body.get('ok'):
            return body
        if 'message is not modified' in str(body.get('description', '')):
            return body
        # Сообщение слишком старое, удалено или было фото — рисуем новым.
        return self._send(chat_id, text, reply_markup)

    def _answer_callback(self, callback_id: str, text: str = ''):
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:190]
        self._api("answerCallbackQuery", payload, timeout=5)

    def _send_document(self, chat_id: str, path: str) -> bool:
        if not config.TELEGRAM_BOT_TOKEN:
            return False
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendDocument"
            with open(path, 'rb') as f:
                r = requests.post(url, data={"chat_id": chat_id},
                                  files={"document": f}, timeout=60)
            return bool(r.json().get('ok'))
        except Exception as e:                         # noqa: BLE001
            log(f"Telegram sendDocument error: {self._safe(e)}")
            return False

    def _set_commands(self):
        if not config.TELEGRAM_BOT_TOKEN:
            return
        commands = [{"command": name, "description": what} for name, what, _code in COMMANDS]
        body = self._api("setMyCommands", {"commands": commands}, timeout=10)
        if body and body.get('ok'):
            log("Telegram команды зарегистрированы")

    # ── Боевой счёт: прежние экраны ──────────────────────────────────────────

    def _legacy_render(self, code):
        """LiveTradeManager: снимка у него нет — показываем то, что было до панели."""
        import telegram_panel as panel
        head = code.split(':')[0]
        if head == 'pl':
            text = self._legacy_positions_text()
        elif head == 'st':
            text = self._legacy_stats_text()
        elif head == 'ai':
            try:
                import strategy_llm
                import telegram_notify as tg
                text = tg.llm_setups_text(strategy_llm.current_setups(self.trade_manager))
            except Exception as e:                     # noqa: BLE001
                text = f"⚠️ Список сетапов не собран: {e}"
        elif head == 'h':
            return ('', *panel.help_view())
        elif head == 'pz':
            self.set_paused(not self.is_paused())
            text = self._legacy_status_text()
        else:
            text = self._legacy_status_text()
        keyboard = panel.keyboard([
            [('⏸ Пауза входов' if not self.is_paused() else '▶️ Снять паузу', 'pz'), ('🔄', 'm')],
            [('📋 Позиции', 'pl:0'), ('📊 Статистика', 'st:7'), ('🤖 Сетапы ИИ', 'ai')],
        ])
        return '', text, keyboard

    def _legacy_status_text(self):
        import tg_format as fmt
        tm = self.trade_manager
        if tm is None:
            return "⚠️ Торговый модуль не запущен"
        balance = tm.get_real_balance()
        pairs = ", ".join(sorted(tm.get_open_pairs())) or "—"
        return (f"<b>📊 Kraken — {fmt.mode_label(config.TRADING_MODE)}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Состояние: {'⏸ Пауза' if self.is_paused() else '▶️ Активен'}\n"
                f"Баланс: <b>${balance:,.2f}</b>\n"
                f"Позиций: {tm.get_active_count()}\n"
                f"Пары: {fmt.esc(pairs)}")

    def _legacy_positions_text(self):
        import tg_format as fmt
        tm = self.trade_manager
        if tm is None:
            return "⚠️ Торговый модуль не запущен"
        lines = []
        for pair, positions in tm.active_positions.items():
            for pos in positions:
                if pos.get('status') != 'OPEN':
                    continue
                opened = fmt.parse_time(pos.get('entry_time'))
                minutes = fmt.minutes_since(opened) or 0
                lines.append(f"{fmt.side_icon(pos['signal']['setup']['type'])} <b>{fmt.esc(pair)}</b> "
                             f"{pos['signal']['setup']['type']} · вход {fmt.price(pos['entry_price'])} · "
                             f"стоп {fmt.price(pos['params']['stop_loss'])} · {fmt.duration(minutes)}")
        return ("<b>📋 Открытые позиции</b>\n" + "\n".join(lines)) if lines else "📭 Открытых позиций нет"

    def _legacy_stats_text(self):
        tm = self.trade_manager
        stats = tm.get_stats_dict() if tm is not None else None
        if not stats:
            return "📊 Статистика пуста — сделок ещё не было."
        pf = stats['profit_factor']
        return (f"<b>📊 Статистика</b>\n"
                f"Сделок: <b>{stats['total']}</b> · WR {stats['win_rate']:.1f}% · "
                f"PF {'∞' if pf == float('inf') else f'{pf:.2f}'}\n"
                f"Итог: <b>${stats['total_pnl']:+.2f}</b> · сегодня ${stats['today_pnl']:+.2f}")

    def _legacy_close_command(self, args, chat_id):
        tm = self.trade_manager
        if tm is None or not args:
            self._send(chat_id, "Укажите пару: /close BTCUSDT")
            return
        pair = args[0].upper()
        if not pair.endswith('USDT'):
            pair += 'USDT'
        if pair not in tm.get_open_pairs():
            self._send(chat_id, f"❓ Позиция <b>{pair}</b> не найдена.")
            return
        success, price = tm.close_position_by_pair(pair)
        self._send(chat_id, f"✅ <b>{pair}</b> закрыта вручную по <code>${price:.4f}</code>"
                   if success else f"⚠️ Не удалось закрыть <b>{pair}</b>. Проверь лог.")


# Один на процесс: bot.py ставит ему брокер и запускает опрос, dashboard и
# telegram_notify спрашивают паузу и «без звука».
controller = BotController()
