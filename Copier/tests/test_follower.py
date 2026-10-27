"""Исполнитель на фальшивых Upscale и боте: сделка участника повторяет счёт бота."""

from copier import crypto, feed
from copier.db import DB
from copier.follower import Follower
from copier.upscale import UpscaleClient, from_fp9, to_fp9
from tests.helpers import FakeBot, FakeUpscale, Notes, book, closed_row, pending, position, run


def setup(tmp_path, level='demo', kind='demo', data=None):
    db = DB(':memory:')
    db.touch_user(42, 'tester', 'Тест')
    db.set_user(42, status='active', level=level)
    key_id = db.add_key(42, crypto.encrypt('usk_secret'), crypto.fingerprint('usk_secret'))
    acc_id = db.add_account(42, key_id, 'acc-1', kind, 10_000, 'active_evaluation')
    fake = FakeUpscale()
    bot = FakeBot(data or book())
    f = feed.Feed(account='upscale', state_path=str(tmp_path / 'source_state.json'), fetch=bot.fetch)
    notes = Notes()
    fol = Follower(db, f, notes, client_factory=lambda key: UpscaleClient(key, send=fake.send))
    return db, fake, bot, f, fol, notes, acc_id


def pump(f, fol):
    async def go():
        for ev in await f.poll():
            await fol.handle(ev)
    run(go())


def sync(db, fol, acc_id):
    run(fol.sync_account(db.account(acc_id)))


def test_full_path(tmp_path):
    o = pending(targets=(110.0, 120.0), fractions=(0.5, 0.5))
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([o]))
    pump(f, fol)

    trades = db.open_trades(account_id=acc_id)
    assert len(trades) == 1 and trades[0]['state'] == 'pending' and trades[0]['strategy'] == 'FIBO'
    entry = next(c for c in fake.calls if c[0] == 'POST')
    assert entry[2]['type'] == 'limit'
    assert entry[2]['stopTriggerPrice'] == to_fp9(95) and 'takeTriggerPrice' not in entry[2]
    assert from_fp9(entry[2]['baseSize']) == 20          # 1% от 10 000 / 5 $
    # Резерв = маржа 20 × 100 / 5 = 400 $ + 3% на комиссию, спред и проскальзывание (AVAX 27.10.2026).
    assert from_fp9(entry[2]['amount']) == 412
    assert any('заявка выставлена' in text for _, text in notes.users)

    # Налив на Upscale → сверка: позиция, два тейка.
    fake.fill(trades[0]['entry_order'])
    sync(db, fol, acc_id)
    t = db.trade(trades[0]['id'])
    assert t['state'] == 'open'
    assert len([x for x in fake.orders.values() if x.get('type') == 'take']) == 2

    # Мастер налился, потом ИИ перевёл в безубыток → стоп участника перенесён.
    bot.data = book([], [position(o)])
    pump(f, fol)
    bot.data = book([], [position(o, stop=100.0)])
    pump(f, fol)
    stop_order = next(x for x in fake.orders.values() if x.get('type') == 'stop')
    assert stop_order['triggerPrice'] == to_fp9(100)

    # ИИ закрыл половину → у участника закрыта половина по рынку.
    bot.data = book([], [position(o, stop=100.0, remaining=0.5, ai_half=True)])
    pump(f, fol)
    pos = next(iter(fake.positions.values()))
    assert int(pos['size']) == int(to_fp9(10))
    assert any('ИИ закрыл половину' in text for _, text in notes.users)

    # Мастер закрыт решением ИИ → остаток закрыт, причина словами.
    bot.data = book([], [], closed=[closed_row(position(o), reason='AI')])
    pump(f, fol)
    t = db.trade(t['id'])
    assert t['state'] == 'closed' and 'решение ИИ' in t['exit_reason']
    assert not fake.positions
    assert t['entry_price'] == '100'                        # notional ÷ size — entryPrice у Upscale нет
    assert float(t['pnl_usd']) == 200 and float(t['exit_price']) == 110   # pnl − fee, exchangedQuote ÷ Base
    assert float(t['pnl_r']) == 2.0


def test_size_follows_each_account(tmp_path):
    """Одна сделка мастера, три счёта 10к / 25к / 100к: риск 1% каждого, размер — пропорционально."""
    from tests.helpers import upscale_account
    db = DB(':memory:')
    fakes = {}
    for tg_id, balance in ((41, 10_000), (42, 25_000), (43, 100_000)):
        db.touch_user(tg_id)
        db.activate(tg_id, 'full')
        key = f'usk_{tg_id}'
        fakes[key] = FakeUpscale(accounts=[upscale_account(f'acc-{tg_id}', balance=balance, kind='real')])
        key_id = db.add_key(tg_id, crypto.encrypt(key), crypto.fingerprint(key))
        db.add_account(tg_id, key_id, f'acc-{tg_id}', 'real', balance, 'active_evaluation')
    bot = FakeBot(book([pending()]))                      # вход 100, стоп 95, риск счёта 1%
    f = feed.Feed(account='upscale', state_path=str(tmp_path / 's.json'), fetch=bot.fetch)
    fol = Follower(db, f, Notes(), client_factory=lambda key: UpscaleClient(key, send=fakes[key].send))
    pump(f, fol)
    sizes = {}
    for key, fake in fakes.items():
        entry = next(c for c in fake.calls if c[0] == 'POST')
        sizes[key] = from_fp9(entry[2]['baseSize'])
    assert sizes == {'usk_41': 20, 'usk_42': 50, 'usk_43': 200}
    risks = sorted(float(t['risk_usd']) for t in db.trades(limit=0))
    assert risks == [100.0, 250.0, 1000.0]


def test_entry_uses_fresh_status_without_extra_request(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path)
    pump(f, fol)
    sync(db, fol, acc_id)                                 # сверка только что обновила снимок
    before = len(fake.calls)
    bot.data = book([pending()])
    pump(f, fol)
    new_calls = [c[1] for c in fake.calls[before:]]
    assert '/accounts/with-risk-status' not in new_calls and '/orders' in new_calls


def test_reduce_skipped_when_take_already_worked(tmp_path):
    o = pending(targets=(110.0, 120.0), fractions=(0.5, 0.5))
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([o]))
    pump(f, fol)
    fake.fill(db.open_trades(account_id=acc_id)[0]['entry_order'])
    sync(db, fol, acc_id)
    pos = next(iter(fake.positions.values()))
    pos['size'] = to_fp9(10)                              # тейк участника уже сработал на Upscale
    bot.data = book([], [position(o)])
    pump(f, fol)
    before = len(fake.calls)
    bot.data = book([], [position(o, tp_hit=1, remaining=0.5)])
    pump(f, fol)
    assert not [c for c in fake.calls[before:] if c[0] == 'POST']


def test_ai_market_entry(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path)
    pump(f, fol)
    o = pending('BTCUSDT', placed_ts=5_000)
    bot.data = book([], [position(o, last_price=102.0)])     # между стопом 95 и целью 110
    pump(f, fol)
    entry = next(c for c in fake.calls if c[0] == 'POST')
    assert entry[2]['type'] == 'market' and 'triggerPrice' not in entry[2]
    assert from_fp9(entry[2]['baseSize']) == Decimal_str('14.285714285')   # 100 $ / 7 $
    assert any('вход по рынку' in text for _, text in notes.users)


def test_ai_market_entry_skipped_beyond_target(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path)
    pump(f, fol)
    o = pending('BTCUSDT', placed_ts=5_000)
    bot.data = book([], [position(o, last_price=111.0)])     # уже за первой целью
    pump(f, fol)
    assert not [c for c in fake.calls if c[0] == 'POST']


def test_stop_entry_becomes_stop_market(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending(stop_entry=True)]))
    pump(f, fol)
    entry = next(c for c in fake.calls if c[0] == 'POST')
    assert entry[2]['type'] == 'stop_market' and entry[2]['triggerPrice'] == to_fp9(100)


def test_master_cancel_cancels_follower(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    pump(f, fol)
    assert len(fake.orders) == 1
    bot.data = book()
    pump(f, fol)
    assert not fake.orders
    t = db.trades(account_id=acc_id)[0]
    assert t['state'] == 'canceled' and t['exit_reason'] == 'мастер снял заявку'


def test_orphan_fill_closed_when_master_cancels(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    pump(f, fol)
    fake.fill(db.open_trades(account_id=acc_id)[0]['entry_order'])
    bot.data = book()
    pump(f, fol)
    assert not fake.positions
    assert db.trades(account_id=acc_id)[0]['state'] == 'closed'


def test_master_stopped_no_entries(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book(status='target'))
    pump(f, fol)
    bot.data = book([pending()], status='target')
    pump(f, fol)
    assert not [c for c in fake.calls if c[0] == 'POST']


def test_observer_and_demo_level(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, level='observer', data=book([pending()]))
    pump(f, fol)
    assert not fake.calls
    db2, fake2, bot2, f2, fol2, _, _ = setup(tmp_path / 'b', kind='real', data=book([pending()]))
    pump(f2, fol2)
    assert not fake2.calls


def test_dead_key_disables_account(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    fake.fail_next = (401, 'session_expired')
    pump(f, fol)
    assert db.account(acc_id)['enabled'] == 0
    assert any('Ключ' in text for _, text in notes.users)


def test_refusal_logged(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending('COTIUSDT')]))
    pump(f, fol)
    assert db.refusals()[0]['reason'] == 'пары нет на Upscale'


def test_ai_note_goes_to_holders_only(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending('BTCUSDT')]))
    pump(f, fol)
    note = {'id': 9, 'kind': 'ai', 'title': 'ИИ: безубыток · BTCUSDT LONG', 'lines': ['прибыль +1.2R'],
            'pair': 'BTCUSDT'}
    other = {'id': 10, 'kind': 'ai', 'title': 'ИИ пропустил сетап · DOTUSDT', 'lines': [], 'pair': 'DOTUSDT'}
    bot.data = book([pending('BTCUSDT')], instructions=[note, other])
    notes.users.clear()
    pump(f, fol)
    assert [text for _, text in notes.users if 'ИИ' in text] == ['🤖 ИИ: безубыток · BTCUSDT LONG\nприбыль +1.2R']


def test_close_account_pauses(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    pump(f, fol)
    fake.fill(db.open_trades(account_id=acc_id)[0]['entry_order'])
    ok, _ = run(fol.close_account(db.account(acc_id), 'tester'))
    assert ok and db.account(acc_id)['paused'] == 1
    assert not fake.positions
    assert not db.open_trades(account_id=acc_id)


def Decimal_str(text):
    from decimal import Decimal
    return Decimal(text)


def test_stop_moves_twice_after_patch_makes_new_order(tmp_path):
    """Upscale на PATCH создаёт новый ордер: второй перенос должен идти по новому id."""
    o = pending()
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([o]))
    pump(f, fol)
    fake.fill(db.open_trades(account_id=acc_id)[0]['entry_order'])
    sync(db, fol, acc_id)
    bot.data = book([], [position(o)])
    pump(f, fol)
    bot.data = book([], [position(o, stop=100.0)])        # безубыток
    pump(f, fol)
    bot.data = book([], [position(o, stop=104.0)])        # ИИ подтянул
    pump(f, fol)
    stops = [x for x in fake.orders.values() if x.get('type') == 'stop']
    assert len(stops) == 1 and stops[0]['triggerPrice'] == to_fp9(104)
    assert db.open_trades(account_id=acc_id)[0]['stop_order'] == stops[0]['id']


def test_leverage_falls_back_when_account_allows_less(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    fake.leverage_allowed = 2                             # счёт вида «2×»
    pump(f, fol)
    posts = [c for c in fake.calls if c[0] == 'POST']
    assert [from_fp9(c[2]['leverage']) for c in posts] == [5, 2]
    t = db.open_trades(account_id=acc_id)[0]
    assert t['state'] == 'pending' and from_fp9(posts[-1][2]['baseSize']) == 20
    # Следующая сделка по тому же рынку сразу с подходящим плечом.
    bot.data = book([pending(), pending('BTCUSDT', placed_ts=9_000)])
    db.update_trade(t['id'], state='canceled')
    before = len(fake.calls)
    pump(f, fol)
    assert [from_fp9(c[2]['leverage']) for c in fake.calls[before:] if c[0] == 'POST'] == [2]


def test_closed_market_refused(tmp_path):
    from tests.helpers import market
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    fake.markets_list = [market('m-btc', 'BTC', closed=True)]
    pump(f, fol)
    assert 'закрыт' in db.refusals()[0]['reason']


def test_entry_canceled_on_execution_reported(tmp_path):
    """AVAX 27.10.2026: Upscale снял заявку в момент налива — причина в сделке, участнику и админу."""
    import time
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    pump(f, fol)
    fake.fail(db.open_trades(account_id=acc_id)[0]['entry_order'])
    fol.now = lambda: time.time() + 600                   # заявка уже не «свежая»
    sync(db, fol, acc_id)
    t = db.trades(account_id=acc_id)[0]
    assert t['state'] == 'canceled' and 'insufficient_reserve_at_execution' in t['exit_reason']
    assert any('insufficient_reserve_at_execution' in text for _, text in notes.users)
    assert any('insufficient_reserve_at_execution' in text for text in notes.admins)


def test_entry_filled_and_closed_between_syncs(tmp_path):
    """Налилась и закрылась стопом Upscale до сверки: сделка закрыта с итогом, не «снята»."""
    import time
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    pump(f, fol)
    pid = fake.fill(db.open_trades(account_id=acc_id)[0]['entry_order'])
    fake.positions.pop(pid)
    fake.orders.clear()                                   # стоп сработал — привязанные сняты
    fol.now = lambda: time.time() + 600
    sync(db, fol, acc_id)
    t = db.trades(account_id=acc_id)[0]
    assert t['state'] == 'closed' and t['position_id'] == pid
    assert float(t['pnl_usd']) == 200 and t['entry_price']


def _index(fake, price):
    for m in fake.markets_list:
        m['state']['indexPrice'] = to_fp9(price)


def test_catch_up_places_waiting_orders_on_activation(tmp_path):
    """Подключился, пока у бота висят заявки без налива: после активации они выставляются и ему."""
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending(), pending('ETHUSDT', placed_ts=2_000)]))
    db.set_user(42, status='pending')                     # ещё на проверке — заявки мастера мимо
    pump(f, fol)
    assert not [c for c in fake.calls if c[0] == 'POST']

    _index(fake, 101)                                     # лонг-лимит 100 ещё ждёт: цена выше
    db.activate(42)
    assert run(fol.catch_up_user(42)) == 2
    posts = [c for c in fake.calls if c[0] == 'POST']
    assert len(posts) == 2 and all(c[2]['type'] == 'limit' for c in posts)
    assert from_fp9(posts[0][2]['triggerPrice']) == 100   # по цене мастера
    assert {t['master_key'] for t in db.open_trades(account_id=acc_id)} == {'BTCUSDT@1000', 'ETHUSDT@2000'}
    assert run(fol.catch_up_user(42)) == 0                # повторно — не дублирует


def test_catch_up_skips_orders_price_already_reached(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending()]))
    db.set_user(42, status='pending')
    pump(f, fol)
    _index(fake, 99.5)                                    # лимит 100 налился бы сразу по рынку
    db.activate(42)
    assert run(fol.catch_up_user(42)) == 0
    assert not [c for c in fake.calls if c[0] == 'POST']
    assert 'при подключении' in db.refusals(limit=5)[0]['reason']


def test_catch_up_stop_entry_waits_below_trigger(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([pending(stop_entry=True)]))
    db.set_user(42, paused=1)                             # на паузе — мимо
    pump(f, fol)
    _index(fake, 99)                                      # стоп-вход лонг 100 ещё не пробит
    db.set_user(42, paused=0)
    assert run(fol.catch_up_user(42)) == 1
    entry = next(c for c in fake.calls if c[0] == 'POST')
    assert entry[2]['type'] == 'stop_market'


def test_catch_up_ignores_open_master_positions(tmp_path):
    o = pending()
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([], [position(o)]))
    db.set_user(42, status='pending')
    pump(f, fol)
    db.activate(42)
    assert run(fol.catch_up_user(42)) == 0
    assert not [c for c in fake.calls if c[0] == 'POST']


def _short(**kw):
    return pending(direction='SHORT', stop=105.0, targets=(90.0,), **kw)


def test_short_position_size_sign(tmp_path):
    """SUI 27.10.2026: у шорта Upscale отдаёт size с минусом — вход, размер и закрытие без знака."""
    o = _short()
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([o]))
    pump(f, fol)
    fake.fill(db.open_trades(account_id=acc_id)[0]['entry_order'])
    sync(db, fol, acc_id)
    t = db.trades(account_id=acc_id)[0]
    assert t['state'] == 'open' and float(t['entry_price']) == 100 and float(t['filled_size']) == 20
    bot.data = book([], [position(o)])
    pump(f, fol)
    bot.data = book([], [], closed=[closed_row(position(o), reason='AI')])
    pump(f, fol)
    close = [c for c in fake.calls if c[0] == 'POST' and c[2].get('type') == 'take' and c[2].get('triggerPrice') == '0']
    assert close and from_fp9(close[-1][2]['amount']) == 20
    assert not fake.positions and db.trade(t['id'])['state'] == 'closed'


def test_open_short_does_not_block_next_entry(tmp_path):
    """Отрицательный вход шорта давал «худший исход $32 120» и отказ по дневному лимиту (AAVE 27.10.2026)."""
    o = _short()
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path, data=book([o]))
    pump(f, fol)
    fake.fill(db.open_trades(account_id=acc_id)[0]['entry_order'])
    sync(db, fol, acc_id)
    bot.data = book([pending('ETHUSDT', placed_ts=2_000)], [position(o)])
    pump(f, fol)
    assert [t for t in db.open_trades(account_id=acc_id) if t['pair'] == 'ETHUSDT']
    assert not db.refusals(limit=5)


def test_limit_already_passed_enters_market(tmp_path):
    """AAVE 27.10.2026: шорт-лимит ниже рынка Upscale не берёт — вход по рынку, риск тот же."""
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path)
    fake.check_trigger = True
    _index(fake, 101)                                     # шорт от 100, цена уже выше
    pump(f, fol)
    bot.data = book([_short()])
    pump(f, fol)
    posts = [c for c in fake.calls if c[0] == 'POST']
    assert [c[2]['type'] for c in posts] == ['limit', 'market']
    assert from_fp9(posts[1][2]['baseSize']) == 25        # 100 $ / (105 − 101)
    t = db.trades(account_id=acc_id)[0]
    assert t['state'] == 'pending' and float(t['limit_price']) == 101 and float(t['risk_usd']) == 100
    assert any('цена уже прошла вход бота' in text for _, text in notes.users)


def test_limit_passed_beyond_stop_not_entered(tmp_path):
    db, fake, bot, f, fol, notes, acc_id = setup(tmp_path)
    fake.check_trigger = True
    _index(fake, 106)                                     # уже за стопом 105
    pump(f, fol)
    bot.data = book([_short()])
    pump(f, fol)
    assert [c[2]['type'] for c in fake.calls if c[0] == 'POST'] == ['limit']
    assert db.trades(account_id=acc_id)[0]['state'] == 'error'
    assert 'по рынку не входим' in db.refusals(limit=1)[0]['reason']
