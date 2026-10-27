"""Админка: вход по токену, участники, источник (счёт бота), пауза входов."""

import json

from aiohttp.test_utils import TestClient, TestServer

from copier import admin_api, config, feed
from copier.db import DB
from copier.follower import Follower
from tests.helpers import FakeBot, Notes, book, pending, position, run


async def _client(tmp_path, data=None):
    db = DB(':memory:')
    bot = FakeBot(data or book())
    f = feed.Feed(account='upscale', state_path=str(tmp_path / 'source_state.json'), fetch=bot.fetch)
    notes = Notes()
    app = admin_api.AdminApp(db, Follower(db, f, notes), f, notes)
    client = TestClient(TestServer(app.app))
    await client.start_server()
    return client, db, notes, bot, f


def test_requires_token(tmp_path):
    async def go():
        client, db, *_ = await _client(tmp_path)
        try:
            assert (await client.get('/api/overview')).status == 401
            bad = await client.get('/api/overview', headers={'X-Admin-Token': 'nope'})
            assert bad.status == 401
            ok = await client.get('/api/overview', headers={'X-Admin-Token': config.ADMIN_TOKEN})
            assert ok.status == 200
            login = await client.get(f'/login?token={config.ADMIN_TOKEN}', allow_redirects=False)
            assert login.status == 302 and 'copier_admin' in login.cookies
            # Сборка страницы: в самой странице и в каждом ответе API — по ней
            # открытое окно узнаёт, что копировщик обновили.
            page = await client.get('/', headers={'X-Admin-Token': config.ADMIN_TOKEN})
            assert admin_api.BUILD in await page.text() and '{{BUILD}}' not in await page.text()
            assert ok.headers['X-Copier-Build'] == admin_api.BUILD
            # Форма с чужой страницы (тот же 127.0.0.1, другой порт — куки уходят) — не JSON, отказ.
            db.touch_user(42, 'p')
            form = await client.post('/api/users/42/activate', data={'level': 'full'},
                                     cookies={'copier_admin': config.ADMIN_TOKEN})
            assert form.status == 415 and db.user(42)['status'] != 'active'
            # Не число вместо номера — 404, а не падение обработчика.
            h = {'X-Admin-Token': config.ADMIN_TOKEN}
            assert (await client.get('/api/accounts/abc', headers=h)).status == 404
        finally:
            await client.close()
    run(go())


def test_users_and_system(tmp_path):
    async def go():
        client, db, notes, *_ = await _client(tmp_path)
        h = {'X-Admin-Token': config.ADMIN_TOKEN, 'Content-Type': 'application/json'}
        try:
            db.touch_user(42, 'p')
            db.set_user(42, upscale_uid='PL-1', status='pending')
            users = await (await client.get('/api/users', headers=h)).json()
            assert users['users'][0]['upscale_uid'] == 'PL-1' and users['users'][0]['status'] == 'pending'

            # Активация из админки: статус, уровень, участнику — следующий шаг.
            r = await client.post('/api/users/42/activate', headers=h, data=json.dumps({'level': 'full'}))
            assert r.status == 200
            assert db.user(42)['status'] == 'active' and db.user(42)['level'] == 'full'
            assert notes.users and 'Доступ открыт' in notes.users[-1][1]

            r = await client.post('/api/users/42', headers=h, data=json.dumps({'max_accounts': '2'}))
            assert r.status == 200 and db.user(42)['max_accounts'] == 2

            # Отключение: статус inactive, участнику — что открытые доведутся.
            r = await client.post('/api/users/42/deactivate', headers=h)
            assert r.status == 200 and db.user(42)['status'] == 'inactive'
            assert 'отключён' in notes.users[-1][1]
            # Галочка без уровня: уровень по проверенному счёту; счёта нет — демо.
            await client.post('/api/users/42/activate', headers=h)
            assert db.user(42)['status'] == 'active' and db.user(42)['level'] == 'demo'

            r = await client.post('/api/system', headers=h, data=json.dumps({'action': 'pause_entries'}))
            assert r.status == 200 and db.entries_paused()
            r = await client.post('/api/system', headers=h, data=json.dumps({'action': 'close_everyone'}))
            assert r.status == 400                      # без слова ЗАКРЫТЬ — нет

            csv = await client.get('/api/trades.csv', headers=h)
            assert csv.status == 200 and 'sep=;' in await csv.text()
        finally:
            await client.close()
    run(go())


def test_source_page_and_switch(tmp_path):
    o = pending('ZECUSDT', direction='SHORT', limit=1240.9, stop=1262.9, targets=(1202.9,))
    data = book([], [position(o)])

    async def go():
        client, db, notes, bot, f = await _client(tmp_path, data)
        h = {'X-Admin-Token': config.ADMIN_TOKEN, 'Content-Type': 'application/json'}
        try:
            src = await (await client.get('/api/source', headers=h)).json()
            assert src['account'] == 'upscale' and src['book']['status'] == 'active'
            assert src['book']['positions'][0]['pair'] == 'ZECUSDT'
            assert src['book']['rules']['ai_control'] is True

            # Другой счёт: книга не отдаётся — источник прежний.
            bot.down = True
            r = await client.post('/api/source', headers=h, data=json.dumps({'account': 'other'}))
            assert r.status == 400 and f.account == 'upscale'
            bot.down = False
            r = await client.post('/api/source', headers=h, data=json.dumps({'account': 'other'}))
            assert r.status == 200 and f.account == 'other' and db.source_account() == 'other'
            assert f.st['snapshot'] is None                # новый источник — с первого взгляда
        finally:
            await client.close()
    run(go())
