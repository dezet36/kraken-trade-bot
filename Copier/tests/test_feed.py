"""Лента: книга торгового счёта бота → события мастера и предохранители."""

import time

from copier import feed
from tests.helpers import FakeBot, book, closed_row, pending, position, run


def make_feed(tmp_path, data=None, now=None):
    bot = FakeBot(data)
    f = feed.Feed(account='upscale', state_path=str(tmp_path / 'source_state.json'),
                  fetch=bot.fetch, now=now or time.time)
    return f, bot


def kinds(events):
    return [e['kind'] for e in events]


def poll(f):
    return run(f.poll())


def test_first_look_offers_pending_only(tmp_path):
    o1, o2 = pending('BTCUSDT'), pending('ETHUSDT', placed_ts=2_000)
    f, bot = make_feed(tmp_path, book([o1], [position(o2)]))
    events = poll(f)
    assert kinds(events) == ['new_order']
    assert events[0]['key'] == 'BTCUSDT@1000'
    assert f.entries_allowed()


def test_order_to_fill_to_close(tmp_path):
    o = pending()
    f, bot = make_feed(tmp_path, book([o]))
    poll(f)
    pos = position(o)
    bot.data = book([], [pos])
    assert kinds(poll(f)) == ['filled']
    # Позиция исчезла, строки закрытия ещё нет — закрытия нет.
    bot.data = book([], [])
    assert kinds(poll(f)) == []
    bot.data = book([], [], closed=[closed_row(pos, reason='AI')])
    events = poll(f)
    assert kinds(events) == ['closed'] and events[0]['trade']['exit_reason'] == 'AI'
    # Отметка закрытых двигается: эту строку больше не просим.
    assert bot.calls[-1][1] == 0 and f.st['closed_ts'] == 2_000_000
    poll(f)
    assert bot.calls[-1][1] == 2_000_000


def test_closed_row_before_position_vanishes(tmp_path):
    o = pending()
    pos = position(o)
    f, bot = make_feed(tmp_path, book([], [pos]))
    poll(f)
    bot.data = book([], [pos], closed=[closed_row(pos)])
    assert kinds(poll(f)) == []
    bot.data = book([], [], closed=[closed_row(pos)])
    assert kinds(poll(f)) == ['closed']


def test_order_gone_stop_moved_and_reductions(tmp_path):
    o1 = pending('BTCUSDT', targets=(110, 120), fractions=(0.5, 0.5))
    o2 = pending('ETHUSDT', placed_ts=2_000)
    o3 = pending('SOLUSDT', placed_ts=3_000)
    f, bot = make_feed(tmp_path, book([o2, o3], [position(o1)]))
    poll(f)
    bot.data = book([o3], [position(o1, stop=100.0, tp_hit=1, remaining=0.5)])
    events = poll(f)
    assert sorted(kinds(events)) == ['order_gone', 'reduced', 'stop_moved']
    red = next(e for e in events if e['kind'] == 'reduced')
    assert red['why'] == 'target' and red['remaining'] == 0.5
    # ИИ закрыл половину оставшегося (tp_hit не вырос).
    bot.data = book([o3], [position(o1, stop=100.0, tp_hit=1, remaining=0.25, ai_half=True)])
    red = poll(f)[0]
    assert red['kind'] == 'reduced' and red['why'] == 'ai_half' and red['remaining'] == 0.25


def test_position_without_order_is_market_entry(tmp_path):
    f, bot = make_feed(tmp_path, book())
    poll(f)
    o = pending('XRPUSDT', placed_ts=5_000)
    bot.data = book([], [position(o, last_price=101.0)])
    events = poll(f)
    assert kinds(events) == ['opened'] and events[0]['rec']['last_price'] == 101.0


def test_stop_entry_is_kept(tmp_path):
    f, bot = make_feed(tmp_path, book([pending(stop_entry=True)]))
    assert poll(f)[0]['rec']['stop_entry'] is True


def test_new_phase_freezes(tmp_path):
    f, bot = make_feed(tmp_path, book([pending()]))
    poll(f)
    bot.data = book([], phase=2)
    assert kinds(poll(f)) == ['freeze']
    assert not f.entries_allowed()
    bot.data = book([pending('ETHUSDT', placed_ts=5)], phase=2)
    assert kinds(poll(f)) == []                           # пока заморожено — событий нет
    ok, _ = run(f.unfreeze())
    assert ok and f.entries_allowed()


def test_mass_vanish_freezes(tmp_path):
    recs = [pending(p, placed_ts=i) for i, p in enumerate(('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT'))]
    f, bot = make_feed(tmp_path, book(recs))
    poll(f)
    bot.data = book(recs[:1])
    assert kinds(poll(f)) == ['freeze']


def test_format_change_freezes(tmp_path):
    f, bot = make_feed(tmp_path, book([pending()]))
    poll(f)
    bot.data = book([pending()], fmt=2)
    events = poll(f)
    assert kinds(events) == ['freeze'] and 'формат' in events[0]['reason']
    assert not f.entries_allowed()


def test_bot_restart_is_stale_then_fresh(tmp_path):
    clock = [time.time()]
    f, bot = make_feed(tmp_path, book(), now=lambda: clock[0])
    poll(f)
    bot.down = True
    clock[0] += 60
    assert kinds(poll(f)) == []                           # минута — ещё не тревога
    clock[0] += 150
    assert kinds(poll(f)) == ['stale'] and not f.entries_allowed()
    assert 'Cannot connect' in f.health()['last_error']
    bot.down = False
    assert 'fresh' in kinds(poll(f)) and f.entries_allowed()


def test_master_stopped_blocks_entries(tmp_path):
    f, bot = make_feed(tmp_path, book())
    poll(f)
    bot.data = book(status='failed')
    events = poll(f)
    assert kinds(events) == ['master_status'] and events[0]['status'] == 'failed'
    assert not f.entries_allowed()


def test_ai_notes_only_new(tmp_path):
    old = {'id': 3, 'kind': 'ai', 'title': 'ИИ пропустил сетап · DOTUSDT', 'lines': ['x'], 'pair': 'DOTUSDT'}
    f, bot = make_feed(tmp_path, book(instructions=[old]))
    assert kinds(poll(f)) == []                           # старое на первом взгляде не шлём
    new = {'id': 4, 'kind': 'ai', 'title': 'ИИ: закрыть половину по рынку · ZECUSDT', 'lines': ['разворот'],
           'pair': 'ZECUSDT'}
    fill = {'id': 5, 'kind': 'fill', 'title': 'Заявка налилась', 'lines': [], 'pair': 'ZECUSDT'}
    bot.data = book(instructions=[old, new, fill])
    events = poll(f)
    assert kinds(events) == ['ai_note'] and events[0]['pair'] == 'ZECUSDT'


def test_restart_keeps_state(tmp_path):
    o = pending()
    f, bot = make_feed(tmp_path, book([o]))
    poll(f)
    # Копировщик перезапущен; за простой заявка налилась.
    f2 = feed.Feed(account='upscale', state_path=str(tmp_path / 'source_state.json'), fetch=bot.fetch)
    bot.data = book([], [position(o)])
    assert kinds(run(f2.poll())) == ['filled']
