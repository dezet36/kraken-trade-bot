"""
Админка копировщика: HTTP на 127.0.0.1:COPIER_ADMIN_PORT, доступ по токену.

Снаружи не открывается: владелец приходит через ssh-туннель (CopierAdmin.exe).
Токен — второй замок на случай, если порт окажется доступен кому-то ещё на
машине: панель торгового бота такого замка не имеет, здесь лежат ключи участников.

Вход: /login?token=… ставит cookie (HttpOnly, SameSite=Strict) → дальше /api/*.
"""

import csv
import hashlib
import hmac
import io
import json
import os
from datetime import datetime, timedelta, timezone

from aiohttp import web

from copier import config, stats
from copier.db import LEVELS, STATUSES
from copier.log import log
from copier.tg import screens
from copier.tg.bot import announce_activation

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin_web')
COOKIE = 'copier_admin'


def _build_id():
    """Отпечаток файлов страницы. Окно, открытое до выкатки, сверяет его и перезагружается."""
    h = hashlib.sha256()
    for name in sorted(os.listdir(WEB_DIR)):
        with open(os.path.join(WEB_DIR, name), 'rb') as fh:
            h.update(name.encode() + fh.read())
    return h.hexdigest()[:12]


# 26.10.2026: окно, открытое до обновления, продолжало исполнять прежний app.js
# и показывало «Ошибка: Not Found» на адресе, которого на сервере уже не было.
BUILD = _build_id()


def _authorized(request):
    token = config.ADMIN_TOKEN
    if not token:
        return False
    given = request.headers.get('X-Admin-Token') or request.cookies.get(COOKIE) or ''
    return hmac.compare_digest(given, token)


@web.middleware
async def auth_mw(request, handler):
    if request.path in ('/login',) or request.path.startswith('/static/'):
        resp = await handler(request)
    elif not _authorized(request):
        if request.path.startswith('/api/'):
            return web.json_response({'error': 'unauthorized'}, status=401)
        return web.Response(text='Нужен вход: /login?token=…', status=401)
    elif request.method not in ('GET', 'HEAD') and request.content_type != 'application/json':
        # Куки SameSite не отличают порты одного 127.0.0.1: чужая страница с localhost
        # могла бы отправить форму. JSON без предварительного запроса CORS не отправить.
        return web.json_response({'error': 'json only'}, status=415)
    else:
        resp = await handler(request)
    # Страница и скрипты — без кэша: после обновления копировщика окно со своим
    # профилем Chrome иначе показывало бы прежнюю админку.
    if request.path == '/' or request.path.startswith('/static/'):
        resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Copier-Build'] = BUILD
    return resp


def _json(data, status=200):
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False,
                                                                              default=str))


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class AdminApp:
    def __init__(self, db, follower, feed, notifier):
        self.db, self.follower, self.feed, self.notifier = db, follower, feed, notifier
        self.app = web.Application(middlewares=[auth_mw])
        r = self.app.router
        r.add_get('/login', self.login)
        r.add_get('/', self.index)
        r.add_static('/static/', WEB_DIR)
        r.add_get('/api/overview', self.overview)
        r.add_get('/api/users', self.users)
        r.add_post(r'/api/users/{tg_id:\d+}', self.user_save)
        r.add_post(r'/api/users/{tg_id:\d+}/activate', self.user_activate)
        r.add_post(r'/api/users/{tg_id:\d+}/deactivate', self.user_deactivate)
        r.add_get('/api/accounts', self.accounts)
        r.add_get(r'/api/accounts/{id:\d+}', self.account)
        r.add_post(r'/api/accounts/{id:\d+}/action', self.account_action)
        r.add_get('/api/source', self.source)
        r.add_post('/api/source', self.source_save)
        r.add_post('/api/source/unfreeze', self.unfreeze)
        r.add_get('/api/trades', self.trades)
        r.add_get('/api/trades.csv', self.trades_csv)
        r.add_post(r'/api/trades/{id:\d+}/close', self.trade_close)
        r.add_get('/api/journal', self.journal)
        r.add_post('/api/system', self.system)

    async def start(self):
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, config.ADMIN_HOST, config.ADMIN_PORT)
        await site.start()
        log(f'админка: http://{config.ADMIN_HOST}:{config.ADMIN_PORT}/')
        return runner

    # ── вход и страница ────────────────────────────────────────────────────
    async def login(self, request):
        token = request.query.get('token', '')
        if not config.ADMIN_TOKEN or not hmac.compare_digest(token, config.ADMIN_TOKEN):
            return web.Response(text='Неверный токен', status=401)
        page = request.query.get('page', '')
        resp = web.HTTPFound('/' + (f'#{page}' if page.isalpha() else ''))
        resp.set_cookie(COOKIE, token, httponly=True, samesite='Strict', max_age=30 * 86400)
        raise resp

    async def index(self, request):
        with open(os.path.join(WEB_DIR, 'index.html'), 'r', encoding='utf-8') as fh:
            page = fh.read().replace('{{BUILD}}', BUILD)
        return web.Response(text=page, content_type='text/html')

    # ── сборка данных ──────────────────────────────────────────────────────
    def _user_name(self, u):
        if not u:
            return '—'
        return f'@{u["username"]}' if u.get('username') else (u.get('first_name') or str(u['tg_id']))

    def _account_row(self, acc, users=None):
        status = self.db.account_status(acc)
        user = (users or {}).get(acc['tg_id']) or self.db.user(acc['tg_id'])
        trades = self.db.trades(account_id=acc['id'], limit=0)
        return {**{k: acc.get(k) for k in ('id', 'tg_id', 'upscale_id', 'kind', 'nominal', 'phase',
                                           'enabled', 'paused', 'status_at', 'last_error',
                                           'created_at')},
                'user': self._user_name(user), 'user_status': (user or {}).get('status'),
                'challenge': stats.challenge(status),
                'risk_per_trade': stats.risk_per_trade(status, self.feed.master_risk_pct()),
                'summary': stats.summarize(trades)}

    async def overview(self, request):
        users = self.db.users()
        accounts = self.db.accounts()
        live = [a for a in accounts if a.get('enabled')]
        all_trades = self.db.trades(limit=0)
        equity = sum(_float(self.db.account_status(a).get('currentEquity')) or 0 for a in live)
        return _json({
            'users': {s: sum(1 for u in users if u['status'] == s) for s in STATUSES},
            'bot_link': self._bot_link(),
            'accounts': {'enabled': len(live), 'total': len(accounts),
                         'demo': sum(1 for a in live if a.get('kind') == 'demo'),
                         'real': sum(1 for a in live if a.get('kind') == 'real')},
            'equity_total': round(equity, 2),
            'periods': stats.periods(all_trades),
            'open': sum(1 for t in all_trades if t['state'] == 'open'),
            'pending': sum(1 for t in all_trades if t['state'] in ('new', 'pending')),
            'feed': self.feed.health(),
            'entries_paused': self.db.entries_paused(),
            'source': self.feed.account,
            'alerts': self._alerts(accounts),
            'exposure': self._exposure(all_trades),
            'risk_pct': self.feed.master_risk_pct(),
        })

    @staticmethod
    def _exposure(trades):
        """Общая позиция по всем участникам: монета и сторона → сколько счетов, риск, стратегии."""
        groups = {}
        for t in trades:
            if t['state'] not in ('open', 'new', 'pending'):
                continue
            g = groups.setdefault((t['pair'], t['direction']), {
                'pair': t['pair'], 'direction': t['direction'], 'open': 0, 'pending': 0,
                'accounts': set(), 'risk_usd': 0.0, 'strategies': set()})
            g['open' if t['state'] == 'open' else 'pending'] += 1
            g['accounts'].add(t['account_id'])
            g['risk_usd'] += _float(t.get('risk_usd')) or 0
            g['strategies'].add(t['strategy'])
        out = []
        for g in groups.values():
            out.append({**g, 'accounts': len(g['accounts']), 'risk_usd': round(g['risk_usd'], 2),
                        'strategies': sorted(g['strategies'])})
        return sorted(out, key=lambda g: -g['risk_usd'])

    def _bot_link(self):
        username = self.db.setting('tg_username')
        return f'https://t.me/{username}' if username else None

    def _alerts(self, accounts):
        out = []
        waiting = self.db.users('pending')
        if waiting:
            out.append(f'На проверке: {len(waiting)} — раздел «Участники»')
        for a in accounts:
            st = self.db.account_status(a)
            if not a.get('enabled'):
                continue
            if st.get('antifraudStatus') not in (None, 'normal'):
                out.append(f'#{a["id"]}: антифрод Upscale — {st.get("antifraudStatus")}')
            if st.get('tradingLockReason'):
                out.append(f'#{a["id"]}: блокировка — {st.get("tradingLockReason")}')
            if a.get('last_error'):
                out.append(f'#{a["id"]}: ошибка — {a["last_error"]}')
        health = self.feed.health()
        if health.get('frozen'):
            out.insert(0, f'Лента счёта-источника заморожена: {health["frozen"]} — раздел «Источник»')
        if health.get('schema'):
            out.insert(0, f'Книга счёта не той схемы: {health["schema"]}')
        if health.get('stale'):
            out.insert(0, f'Книга счёта {health["account"]} не отвечает — новые входы остановлены')
        elif health.get('master_status') and health['master_status'] != 'active':
            out.insert(0, f'Счёт-источник {health["account"]}: {health["master_status"]} — новых входов нет')
        return out

    async def users(self, request):
        users = self.db.users()
        out = []
        for u in users:
            accounts = self.db.accounts(tg_id=u['tg_id'])
            trades = self.db.trades(tg_id=u['tg_id'], limit=0)
            live = [a for a in accounts if a.get('enabled')]
            out.append({**u, 'name': self._user_name(u),
                        'accounts': len(live),
                        'account_list': [{k: a.get(k) for k in ('id', 'kind', 'nominal', 'phase')}
                                         for a in live],
                        'key_users': self.db.key_users(u['tg_id']),
                        'summary': stats.summarize(trades)})
        # Ждущие активации — первыми: это то, ради чего сюда заходят.
        order = {'pending': 0, 'active': 1, 'new': 2, 'inactive': 3}
        out.sort(key=lambda x: (order.get(x['status'], 9), x.get('created_at') or ''))
        return _json({'users': out, 'levels': LEVELS, 'statuses': STATUSES,
                      'bot_link': self._bot_link(),
                      'signup_url': config.SIGNUP_URL})

    async def user_activate(self, request):
        tg_id = int(request.match_info['tg_id'])
        u = self.db.user(tg_id)
        if not u:
            return _json({'error': 'нет такого участника'}, 404)
        body = await request.json() if request.can_read_body else {}
        self.db.activate(tg_id, body.get('level'))
        self.db.audit('admin', 'activate', {'user': tg_id, 'level': self.db.user(tg_id)['level']})
        await announce_activation(self.notifier, tg_id)
        placed = await self.follower.catch_up_user(tg_id)
        return _json({'ok': True, 'placed': placed})

    async def user_deactivate(self, request):
        tg_id = int(request.match_info['tg_id'])
        u = self.db.user(tg_id)
        if not u:
            return _json({'error': 'нет такого участника'}, 404)
        was = u['status']
        self.db.deactivate(tg_id)
        self.db.audit('admin', 'deactivate', {'user': tg_id, 'was': was})
        await self.notifier.user(tg_id, screens.DEACTIVATED if was == 'active' else screens.DECLINED)
        return _json({'ok': True})

    async def user_save(self, request):
        tg_id = int(request.match_info['tg_id'])
        if not self.db.user(tg_id):
            return _json({'error': 'нет такого участника'}, 404)
        body = await request.json()
        fields = {}
        if body.get('level') in LEVELS:
            fields['level'] = body['level']
        if 'max_accounts' in body and str(body['max_accounts']).isdigit():
            fields['max_accounts'] = max(0, int(body['max_accounts']))
        if 'notes' in body:
            fields['notes'] = str(body['notes'])[:1000]
        if 'paused' in body:
            fields['paused'] = 1 if body['paused'] else 0
        self.db.set_user(tg_id, **fields)
        self.db.audit('admin', 'user_save', {'user': tg_id, **{k: v for k, v in fields.items()}})
        return _json({'ok': True})

    async def accounts(self, request):
        users = {u['tg_id']: u for u in self.db.users()}
        return _json({'accounts': [self._account_row(a, users) for a in self.db.accounts()]})

    async def account(self, request):
        acc = self.db.account(int(request.match_info['id']))
        if not acc:
            return _json({'error': 'нет счёта'}, 404)
        row = self._account_row(acc)
        row['status'] = self.db.account_status(acc)
        row['trades'] = self.db.trades(account_id=acc['id'], limit=300)
        row['equity'] = self.db.equity_series(acc['id'])
        row['refusals'] = self.db.refusals(limit=100, account_id=acc['id'])
        row['by_strategy'] = stats.by([t for t in row['trades'] if t['state'] == 'closed'], 'strategy')
        return _json(row)

    async def account_action(self, request):
        acc = self.db.account(int(request.match_info['id']))
        if not acc:
            return _json({'error': 'нет счёта'}, 404)
        action = (await request.json()).get('action')
        if action in ('pause', 'resume'):
            self.db.set_account(acc['id'], paused=1 if action == 'pause' else 0)
            if action == 'resume':
                await self.follower.catch_up(acc)
        elif action in ('enable', 'disable'):
            self.db.set_account(acc['id'], enabled=1 if action == 'enable' else 0)
            if action == 'enable':
                await self.follower.catch_up(acc)
        elif action == 'sync':
            await self.follower.sync_account(acc)
        elif action == 'close_all':
            ok, why = await self.follower.close_account(acc, 'admin')
            if not ok:
                return _json({'error': why}, 400)
            await self.notifier.user(acc['tg_id'], f'🛑 Владелец закрыл все позиции на счёте #{acc["id"]}, '
                                                   f'счёт на паузе.')
        else:
            return _json({'error': 'неизвестное действие'}, 400)
        self.db.audit('admin', f'account_{action}', {'account': acc['id']})
        return _json({'ok': True})

    async def source(self, request):
        """Счёт-источник: живая книга из бота, здоровье ленты, итоги копий по стратегиям."""
        book, error = self.feed.last_book, None
        if not book:
            try:
                book = await self.feed._fetch(0)
            except Exception as exc:                   # noqa: BLE001
                error = str(exc)[:300]
        trades = self.db.trades(limit=0)
        mine = [t for t in trades if t['state'] == 'closed']
        book_view = None
        if book:
            book_view = {k: book.get(k) for k in ('account', 'status', 'status_why', 'phase',
                                                  'phase_started_at', 'start_balance', 'balance',
                                                  'equity', 'room', 'rules', 'at')}
            book_view['pending'] = book.get('pending') or []
            book_view['positions'] = book.get('positions') or []
            book_view['instructions'] = list(reversed(book.get('instructions') or []))[:30]
            book_view['closed'] = list(reversed(book.get('closed') or []))[:30]
        return _json({'account': self.feed.account, 'url': config.SOURCE_URL, 'feed': self.feed.health(),
                      'book': book_view, 'error': error,
                      'followers': stats.summarize(trades), 'by_strategy': stats.by(mine, 'strategy'),
                      'divergence': self._divergence(trades)})

    @staticmethod
    def _divergence(trades):
        """Где участники разошлись с мастером: заявка не налилась, «сироты», ошибки."""
        return {
            'missed': sum(1 for t in trades if t['state'] == 'canceled'
                          and 'мастер закрыл' in (t.get('exit_reason') or '')),
            'orphans': sum(1 for t in trades if 'без налива' in (t.get('exit_reason') or '')),
            'errors': sum(1 for t in trades if t['state'] == 'error'),
        }

    async def source_save(self, request):
        """Другой счёт бота как источник: проверяем, что книга отдаётся, и начинаем с нуля."""
        code = str((await request.json()).get('account') or '').strip()
        if not code or not code.replace('_', '').replace('-', '').isalnum():
            return _json({'error': 'код счёта: буквы, цифры, - и _'}, 400)
        old = self.feed.account
        self.feed.account = code
        try:
            book = await self.feed._fetch(0)
        except Exception as exc:                       # noqa: BLE001
            self.feed.account = old
            return _json({'error': f'книга счёта «{code}» не получена: {str(exc)[:200]}'}, 400)
        if book.get('format') != 1:
            self.feed.account = old
            return _json({'error': f'формат книги {book.get("format")!r} не поддерживается'}, 400)
        self.feed.set_account(code)
        self.db.set_setting('source_account', code)
        self.db.audit('admin', 'source', {'from': old, 'to': code})
        return _json({'ok': True, 'account': code})

    def _filtered_trades(self, request):
        q = request.query
        since = None
        if q.get('days', '').isdigit():
            since = (datetime.now(timezone.utc) - timedelta(days=int(q['days']))).isoformat(timespec='seconds')
        states = q.get('state').split(',') if q.get('state') else None
        return self.db.trades(account_id=int(q['account']) if q.get('account', '').isdigit() else None,
                              tg_id=int(q['user']) if q.get('user', '').lstrip('-').isdigit() else None,
                              strategy=q.get('strategy') or None, states=states, since=since,
                              limit=int(q.get('limit', 1000)) if q.get('limit', '').isdigit() else 1000)

    async def trades(self, request):
        users = {u['tg_id']: self._user_name(u) for u in self.db.users()}
        rows = self._filtered_trades(request)
        for t in rows:
            t['user'] = users.get(t['tg_id'], t['tg_id'])
        return _json({'trades': rows, 'summary': stats.summarize(rows)})

    async def trades_csv(self, request):
        rows = self._filtered_trades(request)
        cols = ['id', 'tg_id', 'account_id', 'strategy', 'pair', 'direction', 'state', 'limit_price',
                'entry_price', 'stop', 'initial_stop', 'exit_price', 'base_size', 'risk_usd', 'pnl_usd',
                'pnl_r', 'exit_reason', 'created_at', 'opened_at', 'closed_at', 'master_key']
        buf = io.StringIO()
        buf.write('sep=;\n')
        w = csv.writer(buf, delimiter=';')
        w.writerow(cols)
        for t in rows:
            w.writerow([str(t.get(c) if t.get(c) is not None else '').replace('.', ',')
                        if c in ('limit_price', 'entry_price', 'stop', 'initial_stop', 'exit_price',
                                 'base_size', 'risk_usd', 'pnl_usd', 'pnl_r') else t.get(c)
                        for c in cols])
        return web.Response(body=('﻿' + buf.getvalue()).encode('utf-8'),
                            content_type='text/csv',
                            headers={'Content-Disposition': 'attachment; filename="copier_trades.csv"'})

    async def trade_close(self, request):
        ok, why = await self.follower.close_trade(int(request.match_info['id']), 'admin')
        return _json({'ok': ok, 'error': why}, 200 if ok else 400)

    async def journal(self, request):
        return _json({'audit': self.db.audit_log(300), 'refusals': self.db.refusals(300),
                      'events': self.db.master_events(300)})

    async def unfreeze(self, request):
        ok, why = await self.feed.unfreeze()
        self.db.audit('admin', 'unfreeze', {'ok': ok, 'why': why})
        return _json({'ok': ok, 'error': why}, 200 if ok else 400)

    async def system(self, request):
        body = await request.json()
        action = body.get('action')
        if action in ('pause_entries', 'resume_entries'):
            self.db.set_setting('entries_paused', action == 'pause_entries')
            if action == 'resume_entries':
                await self.follower.catch_up_all()
        elif action == 'close_everyone':
            if body.get('confirm') != 'ЗАКРЫТЬ':
                return _json({'error': 'нужно подтверждение словом ЗАКРЫТЬ'}, 400)
            self.db.set_setting('entries_paused', True)
            failed = []
            for acc in self.db.accounts(enabled_only=True):
                ok, why = await self.follower.close_account(acc, 'admin')
                if not ok:
                    failed.append(f'#{acc["id"]}: {why}')
            self.db.audit('admin', 'close_everyone', {'failed': failed})
            return _json({'ok': not failed, 'failed': failed})
        elif action == 'broadcast':
            text = str(body.get('text') or '').strip()
            if not text:
                return _json({'error': 'пустой текст'}, 400)
            sent = 0
            for u in self.db.users('active'):
                await self.notifier.user(u['tg_id'], '📣 ' + text[:3500])
                sent += 1
            self.db.audit('admin', 'broadcast', {'sent': sent, 'text': text[:200]})
            return _json({'ok': True, 'sent': sent})
        else:
            return _json({'error': 'неизвестное действие'}, 400)
        self.db.audit('admin', action)
        return _json({'ok': True})
