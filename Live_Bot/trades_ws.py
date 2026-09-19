"""
Полная лента сделок по веб-сокету — дельта агрессора без дыр.

ЗАЧЕМ. Дельта собиралась опросом: тысяча последних сделок раз в четыре
минуты. У BTC тысяча сделок — это одна-две минуты, и покрытие ряда вышло
28% минут (у ETH 17%). Модель читала «−18% за час» как факт, а число стояло
на пятнадцати минутах из шестидесяти — и 15 из 19 отказов 19 сентября 2026
опирались именно на него. Поток `publicTrade` Bybit отдаёт каждую сделку в
момент её совершения: покрытие 100%, и число становится тем, чем называется.

ТОТ ЖЕ ФАЙЛ, ТОТ ЖЕ ВИД СТРОКИ. Поминутные корзины пишутся в
positioning/delta.jsonl в том же формате, что писал опрос
({'ts', 'pair', 'value', 'buy', 'sell', 'n'}): все читатели остаются как есть.
Пока поток жив, опрос дельты молчит (см. positioning.collect_if_due) —
иначе одна минута считалась бы дважды. Упал поток — опрос возвращается сам.

СВЯЗЬ И ПАДЕНИЯ — как у liquidations.py: своё соединение, переподключение с
нарастающей паузой, ничего не роняет. Два сокета вместо одного намеренно:
лента сделок — сотни сообщений в секунду, и её обрыв не должен трогать
редкий поток ликвидаций.

МИНУТА ЗАПИСЫВАЕТСЯ ПО ЗАКРЫТИИ. Текущая минута ещё копится, и записать её
значило бы сохранить половину как целую. Та же дисциплина, что у свечей.
"""

import json
import os
import threading
import time

import config
from logger import log

PATH = os.path.join(config.DATA_DIR, 'positioning', 'delta.jsonl')
WS_URL = os.getenv('BYBIT_WS_PUBLIC', 'wss://stream.bybit.com/v5/public/linear')
PING_SEC = 20
BUCKET_MS = 60_000

# Поток считается живым, если событие приходило не позже этого. На двадцати
# парах сделки идут каждую секунду; минута тишины — обрыв, а не рынок.
ALIVE_SEC = 120

_lock = threading.Lock()
_thread = None
_wanted = {}            # pair -> биржевой id
_buckets = {}           # (pair, minute_ms) -> {'buy','sell','n'}
_pending = threading.Event()
_stats = {'events': 0, 'connected': False, 'since': None, 'last_event': 0,
          'reconnects': 0, 'error': '', 'written': 0}


def ensure_running(pairs, client=None):
    """Поднимает поток, если он не идёт, и обновляет список пар."""
    global _thread
    ids = {}
    for pair in pairs or []:
        ids[pair] = _market_id(pair, client)
    with _lock:
        changed = ids != _wanted
        _wanted.clear()
        _wanted.update(ids)
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_run, name='trades-ws', daemon=True)
        _thread.start()
        log('   лента сделок: поток сбора запущен')
    elif changed:
        _pending.set()


def healthy(now=None):
    """Жив ли поток: соединение есть и сделки приходили только что."""
    now = now if now is not None else time.time()
    with _lock:
        return bool(_stats['connected'] and _stats['last_event']
                    and now - _stats['last_event'] / 1000 <= ALIVE_SEC)


def _market_id(pair, client):
    if client is None:
        return pair
    try:
        import exchange
        symbol = exchange.market_symbol(pair, client)
        market = client.market(symbol) if symbol else None
        return (market or {}).get('id') or pair
    except Exception:                                  # noqa: BLE001
        return pair


def _run():
    import asyncio
    backoff = 2
    while True:
        try:
            asyncio.run(_session())
            backoff = 2
        except Exception as exc:                       # noqa: BLE001
            with _lock:
                _stats['error'] = str(exc)[:200]
                _stats['connected'] = False
                _stats['reconnects'] += 1
            log(f'   лента сделок: соединение оборвалось — {exc}; повтор через {backoff} с')
        flush(force=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)


async def _session():
    import asyncio

    import aiohttp

    subscribed = set()
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.ws_connect(WS_URL, heartbeat=None, timeout=30,
                                      max_msg_size=4 * 1024 * 1024) as ws:
            with _lock:
                _stats['connected'] = True
                _stats['since'] = _stats['since'] or int(time.time() * 1000)
                _stats['error'] = ''
            last_ping = time.time()
            last_flush = time.time()

            async def subscribe_missing():
                with _lock:
                    want = set(_wanted.values())
                missing = sorted(want - subscribed)
                for i in range(0, len(missing), 10):
                    chunk = missing[i:i + 10]
                    await ws.send_json({'op': 'subscribe',
                                        'args': [f'publicTrade.{s}' for s in chunk]})
                    subscribed.update(chunk)
                _pending.clear()

            await subscribe_missing()
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=PING_SEC)
                except asyncio.TimeoutError:
                    msg = None
                now = time.time()
                if msg is not None:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                      aiohttp.WSMsgType.CLOSE):
                        raise ConnectionError(f'сокет закрыт: {msg.type}')
                if now - last_ping >= PING_SEC:
                    await ws.send_json({'op': 'ping'})
                    last_ping = now
                if _pending.is_set():
                    await subscribe_missing()
                if now - last_flush >= 15:
                    flush()
                    last_flush = now


def handle(text, now_ms=None):
    """Одно сообщение потока: сделки раскладываются по минутным корзинам."""
    try:
        data = json.loads(text)
    except ValueError:
        return
    topic = data.get('topic') or ''
    if not topic.startswith('publicTrade.'):
        if data.get('op') == 'subscribe' and data.get('success') is False:
            log(f'⚠️ лента сделок: биржа отвергла подписку — {data.get("ret_msg")}')
        return
    with _lock:
        by_id = {v: k for k, v in _wanted.items()}
    rows = data.get('data') or []
    if isinstance(rows, dict):
        rows = [rows]
    count = 0
    latest = 0
    with _lock:
        for row in rows:
            try:
                symbol = row.get('s') or topic.split('.', 1)[1]
                ts = int(row.get('T') or data.get('ts') or (now_ms or time.time() * 1000))
                amount = float(row.get('v'))
            except (TypeError, ValueError):
                continue
            pair = by_id.get(symbol, symbol)
            key = (pair, ts // BUCKET_MS * BUCKET_MS)
            cell = _buckets.setdefault(key, {'buy': 0.0, 'sell': 0.0, 'n': 0})
            # S — сторона ТЕЙКЕРА (агрессора): Buy — купили по рынку.
            if row.get('S') == 'Buy':
                cell['buy'] += amount
            else:
                cell['sell'] += amount
            cell['n'] += 1
            count += 1
            latest = max(latest, ts)
        if count:
            _stats['events'] += count
            _stats['last_event'] = max(_stats['last_event'], latest)


def flush(force=False, now_ms=None):
    """
    Записывает закрытые минуты. Возвращает, сколько строк записано.

    force — записать и текущую минуту (при обрыве соединения: лучше
    неполная минута с пометкой, чем потерянная). В обычном режиме текущая
    минута ждёт своего закрытия.
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    current = now_ms // BUCKET_MS * BUCKET_MS
    with _lock:
        ready = [k for k in _buckets if force or k[1] < current]
        rows = []
        for pair, minute in sorted(ready, key=lambda k: k[1]):
            cell = _buckets.pop((pair, minute))
            rows.append({'ts': minute, 'pair': pair,
                         'value': round(cell['buy'] - cell['sell'], 6),
                         'buy': round(cell['buy'], 6), 'sell': round(cell['sell'], 6),
                         'n': cell['n'], 'src': 'ws'})
    if not rows:
        return 0
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, 'a', encoding='utf-8') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError as exc:
        log(f'⚠️ лента сделок: файл не записан — {exc}')
        return 0
    with _lock:
        _stats['written'] += len(rows)
    return len(rows)


def stats():
    with _lock:
        return dict(_stats, pairs=len(_wanted), buffered=len(_buckets))
