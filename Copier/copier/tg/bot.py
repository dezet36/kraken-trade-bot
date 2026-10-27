"""
Telegram-бот участников. Путь (решение владельца 26.10.2026; Upscale дал разрешение):
  /start — регистрация в боте, приветствие и ссылка на регистрацию в Upscale;
  «Готово» → ID на Upscale → API-ключ → выбор счёта (если их несколько) →
  данные на проверке у владельца (pending) → активация галочкой в админке или
  кнопкой здесь → бот торгует на счёте; меню: счёт, статистика, пауза, закрытие.

Владелец (COPIER_ADMIN_IDS) в этом же боте видит карточки проверки (/requests).
Подтверждения с меткой времени и сроком жизни — как в торговом боте
(control/telegram_bot.py: _stamp/_fresh): нажатие, пролежавшее в очереди, не
закроет позицию через полчаса.
"""

import asyncio
import re
import time

from copier import config, crypto
from copier.db import account_name, now_iso
from copier.log import log
from copier.tg import screens
from copier.tg.api import TgApi
from copier.upscale import UpscaleClient, UpscaleError, from_fp9

COMMANDS = (('start', 'Начать / где я сейчас'),
            ('menu', 'Меню: счета, позиции, статистика'),
            ('stats', 'Статистика сделок'),
            ('help', 'Как подключить счёт'))

# ID на Upscale: одно слово — буквы, цифры, _ . - (формат в интерфейсе не описан).
UID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{1,63}$')


def _stamp(now=None):
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
    return 0 <= age <= config.CONFIRM_TTL_S


class Notifier:
    """Сообщения исполнителя: участнику и владельцу."""

    def __init__(self, api):
        self.api = api

    async def user(self, tg_id, text, buttons=None):
        if self.api.enabled:
            await self.api.send(tg_id, text, buttons)

    async def admin(self, text, buttons=None):
        if not self.api.enabled:
            log(f'админу: {text}')
            return
        for admin_id in config.ADMIN_IDS:
            await self.api.send(admin_id, text, buttons)


class TgBot:
    def __init__(self, db, follower, feed, api=None, client_factory=None):
        self.db = db
        self.follower = follower
        self.feed = feed
        self.api = api or TgApi()
        self.client_factory = client_factory or (lambda key: UpscaleClient(key))
        self._username = None
        self._running = False

    # ── цикл ───────────────────────────────────────────────────────────────
    async def run(self):
        if not self.api.enabled:
            log('Telegram участников выключен: нет COPIER_TG_TOKEN')
            return
        me = await self.api.me() or {}
        self._username = me.get('username')
        if self._username:
            self.db.set_setting('tg_username', self._username)
        await self.api.set_commands(COMMANDS)
        log(f'Telegram участников: @{self._username}')
        await Notifier(self.api).admin(f'🟢 Копировщик запущен. Копируется счёт бота '
                                       f'«{self.feed.account}». Команды владельца: /admin')
        self._running = True
        offset = int(self.db.setting('tg_offset', 0) or 0)
        while self._running:
            updates = await self.api.updates(offset)
            if not updates:
                await asyncio.sleep(0.5)
            for upd in updates:
                offset = max(offset, int(upd['update_id']) + 1)
                self.db.set_setting('tg_offset', offset)
                try:
                    await self.handle_update(upd)
                except Exception as exc:               # noqa: BLE001
                    log(f'telegram: обработка {upd.get("update_id")} — {exc!r}')

    def stop(self):
        self._running = False

    async def handle_update(self, upd):
        if 'message' in upd:
            msg = upd['message']
            if (msg.get('chat') or {}).get('type') != 'private':
                return
            await self.on_message(msg)
        elif 'callback_query' in upd:
            cq = upd['callback_query']
            msg = cq.get('message') or {}
            if (msg.get('chat') or {}).get('type') not in (None, 'private'):
                return
            await self.on_callback(cq)

    # ── помощники ──────────────────────────────────────────────────────────
    @staticmethod
    def is_admin(tg_id):
        return int(tg_id) in config.ADMIN_IDS

    async def show(self, chat_id, screen, message_id=None):
        text, rows = screen
        if message_id:
            done = await self.api.edit(chat_id, message_id, text, rows)
            if done is not None:
                return
        await self.api.send(chat_id, text, rows)

    def _statuses(self, accounts):
        return {a['id']: self.db.account_status(a) for a in accounts}

    def _user_view(self, u):
        u = dict(u)
        u['_source'] = self.feed.account
        u['_risk'] = self.feed.master_risk_pct()
        return u

    def main_screen(self, u):
        accounts = self.db.accounts(tg_id=u['tg_id'], enabled_only=True)
        return screens.main_menu(self._user_view(u), accounts, self._statuses(accounts))

    # ── сообщения ──────────────────────────────────────────────────────────
    async def on_message(self, msg):
        frm = msg.get('from') or {}
        tg_id = int(frm.get('id'))
        chat_id = msg['chat']['id']
        text = (msg.get('text') or '').strip()
        u = self.db.touch_user(tg_id, frm.get('username'), frm.get('first_name'))
        if self.is_admin(tg_id) and u['status'] != 'active':
            self.db.set_user(tg_id, status='active', level='full', approved_at=now_iso())
            u = self.db.user(tg_id)

        state, _ = self.db.tg_state(tg_id)
        plain = bool(text) and not text.startswith('/')
        if text.startswith('usk_') or (state == 'await_key' and plain):
            # Ключ — первым делом убрать из переписки.
            await self.api.delete(chat_id, msg['message_id'])
            await self.on_key(u, chat_id, text)
            return

        cmd = text.split(' ', 1)[0].split('@')[0].lower()
        if cmd == '/start':
            await self.on_start(u, chat_id)
        elif u['status'] == 'new' and plain and (state == 'await_id' or not u.get('upscale_uid')):
            await self.on_uid(u, chat_id, text)
        elif cmd in ('/menu', '/stats', '/help') and u['status'] == 'active':
            if cmd == '/stats':
                await self.show(chat_id, screens.stats_view(self.db.trades(tg_id=tg_id, limit=0)))
            elif cmd == '/help':
                await self.api.send(chat_id, screens.KEY_HELP)
            else:
                await self.show(chat_id, self.main_screen(u))
        elif self.is_admin(tg_id) and cmd == '/admin':
            health = self.feed.health()
            count = {s: len(self.db.users(s)) for s in ('new', 'pending', 'active', 'inactive')}
            await self.api.send(chat_id, (
                'Команды владельца:\n'
                '/requests — кто на проверке\n'
                '/menu — ваше меню участника\n\n'
                f'Ссылка на бота: https://t.me/{self._username or "?"}\n'
                f'Активных {count["active"]}, на проверке {count["pending"]}, '
                f'регистрируются {count["new"]}, отключено {count["inactive"]}\n'
                f'Источник: счёт бота «{health["account"]}» — {health["master_status"] or "—"}\n'
                f'У мастера: заявок {health["pending"]}, позиций {health["positions"]}'
                + (f', ⚠️ {health["frozen"] or health["schema"]}' if health['frozen'] or health['schema'] else '')
                + (', ⏳ молчит' if health['stale'] else '')
                + '\n\nАктивация, счета и статистика — также в окне CopierAdmin.'))
        elif self.is_admin(tg_id) and cmd == '/requests':
            pending = self.db.users('pending')
            if not pending:
                await self.api.send(chat_id, 'На проверке никого нет.')
            for x in pending:
                await self.show(chat_id, self.review_card(x))
        elif u['status'] == 'active':
            await self.show(chat_id, self.main_screen(u))
        else:
            await self.on_start(u, chat_id)

    async def on_start(self, u, chat_id):
        """Куда вести человека — по тому, где он на пути: Upscale → ID → ключ → проверка → меню."""
        tg_id = u['tg_id']
        if u['status'] == 'inactive':
            await self.api.send(chat_id, screens.DEACTIVATED)
        elif u['status'] == 'pending':
            await self.api.send(chat_id, screens.UNDER_REVIEW)
        elif u['status'] == 'new':
            if u.get('upscale_uid'):
                self.db.set_tg_state(tg_id, 'await_key')
                await self.api.send(chat_id, screens.ask_key(u['upscale_uid']))
            else:
                self.db.set_tg_state(tg_id, None)
                await self.show(chat_id, screens.welcome(u.get('first_name'), config.SIGNUP_URL))
        elif not self.db.accounts(tg_id=tg_id, enabled_only=True) and u.get('level') != 'observer':
            self.db.set_tg_state(tg_id, 'await_key')
            await self.api.send(chat_id, screens.KEY_HELP)
        else:
            await self.show(chat_id, self.main_screen(u))

    async def on_ready(self, u, chat_id):
        """«Готово» после регистрации на Upscale — спрашиваем ID."""
        self.db.set_tg_state(u['tg_id'], 'await_id')
        await self.api.send(chat_id, screens.ASK_ID)

    async def on_uid(self, u, chat_id, text):
        """ID на Upscale записан — дальше API-ключ."""
        uid = text.strip()
        if not UID_RE.match(uid):
            await self.api.send(chat_id, screens.BAD_ID)
            return
        self.db.set_user(u['tg_id'], upscale_uid=uid, uid_at=now_iso())
        self.db.audit(u['tg_id'], 'upscale_id', {'id': uid})
        self.db.set_tg_state(u['tg_id'], 'await_key')
        await self.api.send(chat_id, screens.ask_key(uid))

    async def on_key(self, u, chat_id, text):
        tg_id = u['tg_id']
        if u['status'] == 'inactive':
            await self.api.send(chat_id, screens.DEACTIVATED)
            return
        if u['status'] == 'new' and not u.get('upscale_uid'):
            self.db.set_tg_state(tg_id, 'await_id')
            await self.api.send(chat_id, screens.ID_FIRST)
            return
        if u.get('level') == 'observer' and u['status'] == 'active':
            await self.api.send(chat_id, 'Ваш уровень — наблюдатель: счёт подключать не нужно.')
            return
        key = text.strip().split()[0]
        if not key.startswith('usk_'):
            await self.api.send(chat_id, 'Это не похоже на ключ Upscale: он начинается с usk_.')
            return
        try:
            items = await self.client_factory(key).accounts()
        except UpscaleError as err:
            await self.api.send(chat_id, f'Upscale не принял ключ: {err.code or err.status}. '
                                         f'Проверьте ключ и что API Trading включён на счёте.')
            return
        found = [{'accountId': str(i.get('accountId')), 'type': i.get('type'),
                  'currentPhase': i.get('currentPhase'), 'status': i.get('status'),
                  'nominal': float(from_fp9(i.get('initialAccountBalance')) or 0)}
                 for i in items if str(i.get('status')) == 'active']
        if not found:
            await self.api.send(chat_id, screens.NO_API_ACCOUNTS)
            return
        owners = sorted({str(i.get('userId')) for i in items if i.get('userId')})
        key_id = self.db.add_key(tg_id, crypto.encrypt(key), crypto.fingerprint(key),
                                 upscale_user=owners[0] if owners else None)
        self.db.audit(tg_id, 'key_added', {'key': key_id, 'fingerprint': crypto.fingerprint(key),
                                           'upscale_user': owners})
        connected = {a['upscale_id'] for a in self.db.accounts(tg_id=tg_id, enabled_only=True)}
        fresh = [i for i in found if i['accountId'] not in connected]
        self.db.set_tg_state(tg_id, 'pick_account', {'key_id': key_id, 'items': found})
        if len(fresh) == 1:
            await self.connect_account(self.db.user(tg_id), chat_id, None, key_id, fresh[0])
        else:
            await self.show(chat_id, screens.pick_accounts(found, connected))

    def review_card(self, u):
        return screens.admin_request(u, self.db.accounts(tg_id=u['tg_id'], enabled_only=True),
                                     self.db.key_users(u['tg_id']))

    # ── кнопки ─────────────────────────────────────────────────────────────
    async def on_callback(self, cq):
        frm = cq.get('from') or {}
        tg_id = int(frm.get('id'))
        msg = cq.get('message') or {}
        chat_id = (msg.get('chat') or {}).get('id', tg_id)
        mid = msg.get('message_id')
        data = cq.get('data') or ''
        await self.api.answer(cq['id'])
        u = self.db.touch_user(tg_id, frm.get('username'), frm.get('first_name'))

        head, _, rest = data.partition(':')
        if head in ('ua', 'ub'):
            if self.is_admin(tg_id):
                await self.on_admin_decision(tg_id, chat_id, mid, data)
            return
        if head == 'pk' and u['status'] in ('new', 'active'):
            await self.on_pick(u, chat_id, mid, rest)
            return
        if data == 'rd' and u['status'] == 'new' and not u.get('upscale_uid'):
            await self.on_ready(u, chat_id)
            return
        if u['status'] != 'active':
            await self.on_start(u, chat_id)
            return

        if data == 'm':
            await self.show(chat_id, self.main_screen(u), mid)
        elif data == 'add':
            self.db.set_tg_state(tg_id, 'await_key')
            await self.api.send(chat_id, screens.KEY_HELP)
        elif data == 'ac':
            accounts = self.db.accounts(tg_id=tg_id, enabled_only=True)
            await self.show(chat_id, screens.accounts_view(accounts, self._statuses(accounts),
                                                           u.get('max_accounts')), mid)
        elif head == 'a':
            acc = self._own_account(u, rest)
            if acc:
                summary = self._summary(acc['id'])
                await self.show(chat_id, screens.account_view(acc, self.db.account_status(acc),
                                                              summary, self.feed.master_risk_pct()), mid)
        elif data == 'st':
            await self.show(chat_id, screens.stats_view(self.db.trades(tg_id=tg_id, limit=0)), mid)
        elif data in ('pl', 'ol'):
            await self.show(chat_id, self._trades_screen(u, data), mid)
        elif head == 'hi':
            page = int(rest) if rest.isdigit() else 0
            await self.show(chat_id, screens.history_view(self.db.trades(tg_id=tg_id, limit=0), page), mid)
        elif data == 'se':
            await self.show(chat_id, self._settings(u), mid)
        elif data == 'ds':
            self.db.set_user(tg_id, daily_summary=0 if u.get('daily_summary') else 1)
            await self.show(chat_id, self._settings(self.db.user(tg_id)), mid)
        elif data == 'pz':
            self.db.set_user(tg_id, paused=0 if u.get('paused') else 1)
            self.db.audit(tg_id, 'resume' if u.get('paused') else 'pause')
            await self.show(chat_id, self.main_screen(self.db.user(tg_id)), mid)
            if u.get('paused'):
                await self.follower.catch_up_user(tg_id)
        elif head == 'ap':
            acc = self._own_account(u, rest)
            if acc:
                self.db.set_account(acc['id'], paused=0 if acc.get('paused') else 1)
                self.db.audit(tg_id, 'account_resume' if acc.get('paused') else 'account_pause',
                              {'account': acc['id']})
                was_paused = acc.get('paused')
                acc = self.db.account(acc['id'])
                await self.show(chat_id, screens.account_view(acc, self.db.account_status(acc),
                                                              self._summary(acc['id']), self.feed.master_risk_pct()), mid)
                if was_paused:
                    await self.follower.catch_up(acc)
        elif head in ('ax', 'ad', 'tc'):
            await self.ask_confirm(u, chat_id, mid, head, rest)
        elif data == 'dx':
            await self.show(chat_id, screens.confirm(
                'Отключиться полностью? Ключи будут стёрты, заявки сняты. Открытые позиции '
                'останутся на Upscale со своими стопами — копировщик их больше не ведёт.',
                f'y:dx:0:{_stamp()}'), mid)
        elif head == 'y':
            await self.on_confirmed(u, chat_id, mid, rest)

    def _summary(self, account_id):
        from copier import stats
        return stats.summarize(self.db.trades(account_id=account_id, limit=0))

    def _own_account(self, u, account_id):
        try:
            acc = self.db.account(int(account_id))
        except (TypeError, ValueError):
            return None
        if acc and (acc['tg_id'] == u['tg_id'] or self.is_admin(u['tg_id'])):
            return acc
        return None

    def _trades_screen(self, u, kind):
        accounts = {a['id']: a for a in self.db.accounts(tg_id=u['tg_id'])}
        if kind == 'pl':
            items = [t for t in self.db.open_trades(tg_id=u['tg_id']) if t['state'] == 'open']
            return screens.trades_view('Открытые позиции:', items, accounts, 'нет', with_close=True)
        items = [t for t in self.db.open_trades(tg_id=u['tg_id']) if t['state'] in ('new', 'pending')]
        return screens.trades_view('Заявки, ждущие налива:', items, accounts, 'нет', with_close=True)

    def _settings(self, u):
        return screens.settings_view(u, self.feed.master_risk_pct())

    async def on_pick(self, u, chat_id, mid, upscale_id):
        state, data = self.db.tg_state(u['tg_id'])
        if state != 'pick_account':
            await self.api.send(chat_id, 'Список устарел — пришлите ключ ещё раз.')
            return
        item = next((i for i in data.get('items', []) if i['accountId'].startswith(upscale_id)), None)
        if item is None:
            return
        await self.connect_account(u, chat_id, mid, data['key_id'], item)

    async def connect_account(self, u, chat_id, mid, key_id, item):
        """
        Счёт подключён к копировщику. Новичок на этом шаге уходит на проверку
        владельцу (pending): торговать бот начнёт только после активации —
        follower.eligible требует status=active.
        """
        tg_id = u['tg_id']
        active = u['status'] == 'active'
        enabled = self.db.accounts(tg_id=tg_id, enabled_only=True)
        if len(enabled) >= int(u.get('max_accounts') or 1):
            await self.api.send(chat_id, f'Предел счетов: {u.get("max_accounts") or 1}. '
                                         f'Отключите другой счёт или попросите поднять предел.')
            return
        if active and u.get('level') == 'demo' and item.get('type') != 'demo':
            await self.api.send(chat_id, 'Ваш уровень доступа — только демо-счета.')
            return
        acc_id = self.db.add_account(tg_id, key_id, item['accountId'], item.get('type'),
                                     item.get('nominal'), item.get('currentPhase'))
        if acc_id is None:
            # Чей он — участнику не говорим. Владельцу — да: один ключ Upscale у двух людей
            # (правила Upscale передачу ключа запрещают).
            await self.api.send(chat_id, 'Этот счёт подключить нельзя. Если он ваш — напишите владельцу бота.')
            other = self.db.q1('SELECT tg_id FROM accounts WHERE upscale_id=?', (item['accountId'],))
            self.db.audit(tg_id, 'account_taken', {'upscale': item['accountId'],
                                                   'owner': other and other['tg_id']})
            await Notifier(self.api).admin(f'⚠️ Telegram {tg_id} пытался подключить счёт Upscale '
                                           f'{item["accountId"]}, который уже подключён у '
                                           f'{other and other["tg_id"]}: один ключ у двух людей?')
            return
        self.db.set_tg_state(tg_id, None)
        self.db.audit(tg_id, 'account_added', {'account': acc_id, 'upscale': item['accountId']})
        acc = self.db.account(acc_id)
        try:
            await self.follower.sync_account(acc)
        except Exception as exc:                       # noqa: BLE001
            log(f'telegram: первая сверка счёта #{acc_id} — {exc!r}')
        if not active:
            self.db.set_user(tg_id, status='pending')
            await self.api.send(chat_id, screens.SUBMITTED)
            text, rows = self.review_card(self.db.user(tg_id))
            await Notifier(self.api).admin(text, rows)
            return
        name = u.get('username') and f'@{u["username"]}' or tg_id
        await Notifier(self.api).admin(f'Участник {name} подключил счёт #{acc_id} '
                                       f'({item.get("type")}, ${item.get("nominal") or 0:,.0f})')
        acc = self.db.account(acc_id)
        await self.show(chat_id, screens.account_view(acc, self.db.account_status(acc),
                                                      self._summary(acc_id), self.feed.master_risk_pct()), mid)
        await self.follower.catch_up(acc)

    async def ask_confirm(self, u, chat_id, mid, head, rest):
        if head == 'tc':
            t = self.db.trade(int(rest)) if rest.isdigit() else None
            if not t or (t['tg_id'] != u['tg_id'] and not self.is_admin(u['tg_id'])):
                return
            text = f'Закрыть {t["pair"]} {"ЛОНГ" if t["direction"] == "LONG" else "ШОРТ"} по рынку ' \
                   f'(или снять заявку)?'
        else:
            acc = self._own_account(u, rest)
            if not acc:
                return
            if head == 'ax':
                text = (f'Закрыть ВСЕ позиции и снять заявки на счёте «{account_name(acc)}»? '
                        f'Счёт встанет на паузу.')
            else:
                text = (f'Отключить счёт «{account_name(acc)}»? Заявки будут сняты. Открытые позиции останутся '
                        f'на Upscale со своими стопами — копировщик их больше не ведёт.')
        await self.show(chat_id, screens.confirm(text, f'y:{head}:{rest}:{_stamp()}'), mid)

    async def on_confirmed(self, u, chat_id, mid, rest):
        parts = rest.split(':')
        if len(parts) != 3 or not _fresh(parts[2]):
            await self.show(chat_id, ('Подтверждение устарело — повторите действие.', [[('« Меню', 'm')]]), mid)
            return
        head, ident = parts[0], parts[1]
        actor = f'tg:{u["tg_id"]}'
        if head == 'tc':
            # callback_data присылает клиент — подделать номер чужой сделки можно, владельца проверяем здесь.
            t = self.db.trade(int(ident)) if ident.isdigit() else None
            if not t or (t['tg_id'] != u['tg_id'] and not self.is_admin(u['tg_id'])):
                return
            ok, why = await self.follower.close_trade(t['id'], actor)
            text = 'Готово.' if ok else f'Не получилось: {why}'
        elif head == 'ax':
            acc = self._own_account(u, ident)
            ok, why = await self.follower.close_account(acc, actor) if acc else (False, 'нет счёта')
            text = 'Всё закрыто, счёт на паузе.' if ok else f'Не получилось: {why}'
        elif head == 'ad':
            acc = self._own_account(u, ident)
            text = await self.disconnect(acc, actor) if acc else 'Нет счёта.'
        elif head == 'dx':
            for acc in self.db.accounts(tg_id=u['tg_id'], enabled_only=True):
                await self.disconnect(acc, actor)
            self.db.forget_keys(u['tg_id'])
            self.db.audit(actor, 'disconnect_all')
            text = 'Вы отключены, ключи стёрты.'
        else:
            return
        await self.show(chat_id, (text, [[('« Меню', 'm')]]), mid)

    async def disconnect(self, acc, actor):
        """Счёт больше не копирует: заявки сняты, позиции остаются под своими стопами."""
        client = self.follower.client(acc)
        for t in self.db.open_trades(account_id=acc['id']):
            if t['state'] in ('new', 'pending') and client is not None:
                await self.follower._cancel_entry(client, t)
                self.db.update_trade(t['id'], state='canceled', closed_at=now_iso(),
                                     exit_reason='счёт отключён')
            elif t['state'] == 'open':
                self.db.update_trade(t['id'], state='closed', closed_at=now_iso(),
                                     exit_reason='счёт отключён — позиция осталась на Upscale')
        self.db.set_account(acc['id'], enabled=0)
        still_used = [a for a in self.db.accounts(tg_id=acc['tg_id'], enabled_only=True)
                      if a['key_id'] == acc['key_id']]
        if not still_used and acc.get('key_id'):
            self.db.x('DELETE FROM keys WHERE id=?', (acc['key_id'],))
            self.follower.forget_client(acc['key_id'])
        self.db.audit(actor, 'account_disconnected', {'account': acc['id']})
        return f'Счёт «{account_name(acc)}» отключён.'

    async def on_admin_decision(self, admin_id, chat_id, mid, data):
        head, _, rest = data.partition(':')
        tg_part, _, level = rest.partition(':')
        try:
            target = int(tg_part)
        except ValueError:
            return
        u = self.db.user(target)
        if not u:
            return
        name = u.get('username') and f'@{u["username"]}' or target
        if head == 'ua' and level in ('observer', 'demo', 'full', 'auto'):
            self.db.activate(target, None if level == 'auto' else level)
            level = self.db.user(target)['level']
            self.db.audit(f'tg:{admin_id}', 'activate', {'user': target, 'level': level})
            await announce_activation(Notifier(self.api), target)
            placed = await self.follower.catch_up_user(target)
            await self.show(chat_id, (f'✅ {name}: доступ открыт ({screens.LEVEL_TITLES[level]})'
                                      + (f', выставлено ждущих заявок бота: {placed}' if placed else ''), []), mid)
        elif head == 'ub':
            was = u['status']
            self.db.deactivate(target)
            self.db.audit(f'tg:{admin_id}', 'deactivate', {'user': target})
            await Notifier(self.api).user(target, screens.DECLINED if was != 'active' else screens.DEACTIVATED)
            await self.show(chat_id, (f'⛔ {name}: доступ не открыт', []), mid)


async def announce_activation(notifier, tg_id):
    """Участнику — доступ открыт, бот торгует; кнопка в меню."""
    await notifier.user(tg_id, screens.ACTIVATED, [[('Открыть меню', 'm')]])
