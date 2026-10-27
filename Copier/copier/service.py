"""
Служба копировщика: книга счёта бота → исполнитель, сверка счетов, Telegram, админка.

Запуск: python -m copier.service  (на сервере — copier.service, User=copier).
Источник — книга торгового счёта бота по HTTP на localhost (решение владельца
27.10.2026, счёт по умолчанию `upscale`). Импортов из бота и чтения его файлов нет.
"""

import asyncio
import signal
from datetime import datetime, timezone

from copier import config, stats
from copier.admin_api import AdminApp
from copier.db import DB, account_name
from copier.feed import Feed
from copier.follower import Follower
from copier.log import log
from copier.tg.api import TgApi
from copier.tg.bot import Notifier, TgBot


async def feed_loop(feed, follower, observers):
    while True:
        try:
            for ev in await feed.poll():
                await follower.handle(ev)
                if ev.get('kind') in ('new_order', 'opened'):
                    await observers(ev)
        except Exception as exc:                       # noqa: BLE001
            log(f'лента: {exc!r}')
        await asyncio.sleep(config.FEED_POLL_S)


async def reconcile_loop(db, follower):
    while True:
        accounts = db.accounts(enabled_only=True)
        for acc in accounts:
            try:
                await follower.sync_account(acc)
            except Exception as exc:                   # noqa: BLE001
                log(f'сверка #{acc["id"]}: {exc!r}')
        await asyncio.sleep(config.RECONCILE_S)


async def daily_loop(db, notifier):
    """Сводка за сутки в 00:05 UTC — тем, у кого она включена."""
    while True:
        now = datetime.now(timezone.utc)
        day = now.strftime('%Y-%m-%d')
        if now.hour == 0 and now.minute >= 5 and db.setting('daily_sent') != day:
            db.set_setting('daily_sent', day)
            for u in db.users('active'):
                if not u.get('daily_summary'):
                    continue
                trades = db.trades(tg_id=u['tg_id'], limit=0)
                s = stats.summarize(stats.since(trades, 1))
                lines = [f'🗞 Сутки: {s["trades"]} сделок · {s["pnl_usd"]:+.2f} $ · {s["sum_r"]:+g}R']
                for acc in db.accounts(tg_id=u['tg_id'], enabled_only=True):
                    c = stats.challenge(db.account_status(acc))
                    if c.get('equity') is not None:
                        lines.append(f'{account_name(acc)}: эквити {c["equity"]:,.2f} $'
                                     + (f' · {c["profit_pct"]:+.2f}%' if c.get('profit_pct') is not None else ''))
                await notifier.user(u['tg_id'], '\n'.join(lines))
        await asyncio.sleep(60)


def make_observers(db, notifier):
    async def observers(ev):
        """Наблюдателям — сделки счёта-мастера, без входа на их счёт."""
        rec = ev.get('rec') or {}
        price = rec.get('limit_price') if ev.get('kind') == 'new_order' else rec.get('entry_price')
        text = (f'👁 {rec.get("strategy")} {rec.get("pair")} '
                f'{"ЛОНГ" if rec.get("direction") == "LONG" else "ШОРТ"}: вход {price}, '
                f'стоп {rec.get("stop_loss")}, цели {", ".join(str(t) for t in rec.get("targets") or [])}')
        for u in db.users('active'):
            if u.get('level') == 'observer':
                await notifier.user(u['tg_id'], text)
    return observers


async def main():
    for line in config.check():
        log(f'⚠️ {line}')
    db = DB()
    feed = Feed(account=db.source_account())
    api = TgApi()
    notifier = Notifier(api)
    follower = Follower(db, feed, notifier)
    bot = TgBot(db, follower, feed, api=api)
    admin = AdminApp(db, follower, feed, notifier)
    runner = await admin.start() if config.ADMIN_TOKEN else None
    log(f'копировщик запущен: источник — счёт бота «{feed.account}» ({config.SOURCE_URL}), входы '
        f'{"на паузе" if db.entries_paused() else "разрешены"}, опрос {config.FEED_POLL_S:g} с, '
        f'сверка {config.RECONCILE_S:g} с; риск — из правил счёта в боте')
    tasks = [asyncio.create_task(feed_loop(feed, follower, make_observers(db, notifier))),
             asyncio.create_task(reconcile_loop(db, follower)),
             asyncio.create_task(daily_loop(db, notifier)),
             asyncio.create_task(bot.run())]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    await stop.wait()
    log('копировщик останавливается')
    bot.stop()
    for t in tasks:
        t.cancel()
    if runner is not None:
        await runner.cleanup()
    await api.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
