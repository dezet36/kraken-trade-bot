"""
Настоящие ликвидации — по веб-сокету, потому что иначе их не получить.

ЗАЧЕМ. Карта ликвидаций — первое, что просят у аналитика, и то, чего нет ни
у одной биржи по REST: Bybit `fetchLiquidations` отвечает None, история не
отдаётся вовсе. Оценку по приросту открытого интереса llm_market строит сама,
и она подписана как оценка. Здесь же копится ФАКТ: поток `allLiquidation`
Bybit отдаёт каждую принудительную ликвидацию по инструменту — цену,
сторону и объём — в момент, когда она случилась.

ИСТОРИИ НЕТ, ПОКА ЕЁ НЕ НАКОПИЛИ. Ряд начинается с первого запуска этого
файла и растёт живьём — как дельта агрессора, которую тот же проект копит
с 18 сентября 2026. Первые сутки в снимке будет «мало данных: сбор идёт N
часов», и это честнее прочерка: модель видит, что источник есть и растёт.

ПОЧЕМУ СВОЙ ПОТОК, А НЕ ЦИКЛ. Ликвидации приходят в любую секунду, и цикл
раз в пять минут их не увидит — событий между опросами у REST просто нет.
Поток один на все пары: одно соединение, подписка на каждую. Соединение
рвётся — переподключаемся с нарастающей паузой и не роняем ничего: бот без
этого потока торгует так же, как торговал вчера.

СТОРОНА. В потоке `allLiquidation` поле S — сторона ПОЗИЦИИ по документации
Bybit: Buy означает, что ликвидирован лонг, Sell — шорт. Это записано в
строке как 'long' / 'short', чтобы читающему не пришлось помнить соглашение.

ФАЙЛ. Строки JSON, по одной на событие, в папке позиционирования рядом с
дельтой: {'ts', 'pair', 'side', 'price', 'size'}. Читается целиком, как и
остальные ряды; за сутки на двадцати парах это тысячи строк, не миллионы.
"""

import json
import os
import threading
import time

import config
from logger import log

PATH = os.path.join(config.DATA_DIR, 'positioning', 'liquidations.jsonl')

WS_URL = os.getenv('BYBIT_WS_PUBLIC', 'wss://stream.bybit.com/v5/public/linear')

# Сколько секунд между записями буфера на диск. События идут пачками, и
# писать каждое отдельно значило бы дёргать диск сотни раз в минуту.
FLUSH_SEC = 5

# Bybit закрывает соединение без пинга дольше ~20 секунд.
PING_SEC = 20

_lock = threading.Lock()
_thread = None
_wanted = {}            # pair -> биржевой id (BTCUSDT), что подписывать
_buffer = []
_stats = {'events': 0, 'connected': False, 'since': None, 'last_event': None,
          'reconnects': 0, 'error': ''}


def ensure_running(pairs, client=None):
    """
    Поднимает поток, если он не идёт, и обновляет список пар.

    Зовётся каждый цикл — это дёшево: поток уже идёт, меняется только словарь
    подписок, а новые пары досылаются при следующем переподключении или
    сразу, если соединение живо (см. _subscribe_pending).
    """
    global _thread
    ids = {}
    for pair in pairs or []:
        ids[pair] = _market_id(pair, client)
    with _lock:
        changed = ids != _wanted
        _wanted.clear()
        _wanted.update(ids)
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_run, name='liquidations', daemon=True)
        _thread.start()
        log('   ликвидации: поток сбора запущен')
    elif changed:
        _pending.set()


def _market_id(pair, client):
    """Биржевой id инструмента: 'SHIB1000USDT' у Bybit, не наш 'SHIB1000USDT'."""
    if client is None:
        return pair
    try:
        import exchange
        symbol = exchange.market_symbol(pair, client)
        market = client.market(symbol) if symbol else None
        return (market or {}).get('id') or pair
    except Exception:                                  # noqa: BLE001
        return pair


_pending = threading.Event()


def _run():
    """Цикл переподключений. Никогда не поднимает исключений наружу."""
    import asyncio
    backoff = 2
    while True:
        try:
            asyncio.run(_session())
            backoff = 2
        except Exception as exc:                       # noqa: BLE001
            _stats['error'] = str(exc)[:200]
            _stats['connected'] = False
            _stats['reconnects'] += 1
            log(f'   ликвидации: соединение оборвалось — {exc}; повтор через {backoff} с')
        _flush()
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)


async def _session():
    import asyncio

    import aiohttp

    subscribed = set()
    # Системный резолвер, а не c-ares: aiohttp по умолчанию берёт aiodns,
    # если тот установлен, и в закрытых сетях он не находит DNS-серверов там,
    # где getaddrinfo находит. REST по ccxt на той же машине работал.
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.ws_connect(WS_URL, heartbeat=None, timeout=30) as ws:
            _stats['connected'] = True
            _stats['since'] = _stats['since'] or int(time.time() * 1000)
            _stats['error'] = ''
            last_ping = time.time()
            last_flush = time.time()

            async def subscribe_missing():
                with _lock:
                    want = set(_wanted.values())
                missing = sorted(want - subscribed)
                # Не больше десяти тем в одном запросе — предел Bybit.
                for i in range(0, len(missing), 10):
                    chunk = missing[i:i + 10]
                    await ws.send_json({'op': 'subscribe',
                                        'args': [f'allLiquidation.{s}' for s in chunk]})
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
                        _handle(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                      aiohttp.WSMsgType.CLOSE):
                        raise ConnectionError(f'сокет закрыт: {msg.type}')
                if now - last_ping >= PING_SEC:
                    await ws.send_json({'op': 'ping'})
                    last_ping = now
                if _pending.is_set():
                    await subscribe_missing()
                if now - last_flush >= FLUSH_SEC:
                    _flush()
                    last_flush = now


def _handle(text):
    """Одно сообщение потока. Всё, что не ликвидация, молча пропускается."""
    try:
        data = json.loads(text)
    except ValueError:
        return
    topic = data.get('topic') or ''
    if not topic.startswith('allLiquidation.'):
        # Отказ в подписке — единственное, о чём стоит сказать вслух: молча
        # он выглядел бы как «ликвидаций не было».
        if data.get('op') == 'subscribe' and data.get('success') is False:
            log(f'⚠️ ликвидации: биржа отвергла подписку — {data.get("ret_msg")}')
        return
    with _lock:
        by_id = {v: k for k, v in _wanted.items()}
    rows = data.get('data') or []
    if isinstance(rows, dict):
        rows = [rows]
    out = []
    for row in rows:
        try:
            symbol = row.get('s') or topic.split('.', 1)[1]
            out.append({
                'ts': int(row.get('T') or data.get('ts') or time.time() * 1000),
                'pair': by_id.get(symbol, symbol),
                # Buy — ликвидирован лонг, Sell — шорт (сторона позиции).
                'side': 'long' if row.get('S') == 'Buy' else 'short',
                'price': float(row.get('p')),
                'size': float(row.get('v')),
            })
        except (TypeError, ValueError):
            continue
    if out:
        with _lock:
            _buffer.extend(out)
            _stats['events'] += len(out)
            _stats['last_event'] = out[-1]['ts']


def _flush():
    """Сбрасывает буфер в файл. Отказ записи не имеет права уронить поток."""
    with _lock:
        rows, _buffer[:] = list(_buffer), []
    if not rows:
        return
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        with open(PATH, 'a', encoding='utf-8') as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    except OSError as exc:
        log(f'⚠️ ликвидации: файл не записан — {exc}')


def stats():
    """Состояние сборщика — для панели."""
    with _lock:
        return dict(_stats, pairs=len(_wanted), buffered=len(_buffer))


# ── Чтение ───────────────────────────────────────────────────────────────────

def rows(pair, since=None, upto=None, path=None):
    """
    События по паре в окне [since, upto], по возрастанию времени.

    upto — отсечка для разбора прошлого, как у positioning.series: без неё
    чтение видело бы будущее.
    """
    path = path or PATH
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('pair') != pair:
                    continue
                ts = int(row.get('ts') or 0)
                if since is not None and ts < since:
                    continue
                if upto is not None and ts > upto:
                    continue
                out.append(row)
    except OSError:
        return []
    out.sort(key=lambda r: r['ts'])
    return out


def first_ts(path=None):
    """Когда началась история — по первой строке файла. None, если файла нет."""
    path = path or PATH
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                try:
                    return int(json.loads(line).get('ts') or 0) or None
                except ValueError:
                    continue
    except OSError:
        return None
    return None
