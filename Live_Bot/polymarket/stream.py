"""
Живой поток стакана: подписка вместо опроса.

ЗАЧЕМ ЭТО ПОНАДОБИЛОСЬ. Заявка теряет край быстрее, чем мы успеваем её снять.
Замер по живым исполнениям: у СТОЯЩИХ котировок край +0.0095, а к моменту
исполнения от него остаётся +0.0005 — двадцатая часть. Между двумя опросами
рынок успевает пройти сквозь нашу цену, и подбирают нас ровно тогда, когда
заявка перестала быть выгодной.

Опросом это не лечится. Такт уже сокращён с тридцати секунд до десяти, и
дальше упирается в предел запросов: каждый такт — это стаканы всех рынков,
сделки, заявки, остаток. Сократить ещё вдесятеро значит вдесятеро увеличить
нагрузку на биржу и получить отказы по лимиту.

Подписка снимает вопрос целиком. Площадка сама присылает изменение стакана в
тот момент, когда оно случилось, и в сообщении `price_change` приходят готовые
`best_bid` и `best_ask` — ровно то, из чего считается край.

ПОЧЕМУ AIOHTTP, А НЕ ОТДЕЛЬНАЯ БИБЛИОТЕКА. Он уже стоит в зависимостях ради
других задач, и добавлять ради одного соединения ещё одну — значит увеличивать
сборку и список того, что может не уехать в .exe. Асинхронность прячется
внутри: наружу модуль отдаёт обычные словари.

ПОТОК НЕ ЕДИНСТВЕННЫЙ ИСТОЧНИК ПРАВДЫ, И ЭТО НАМЕРЕННО. Соединение рвётся,
подписка отстаёт от списка рынков, площадка перезапускает сервер. Поэтому
опрос остаётся на месте, а поток лишь ОПЕРЕЖАЕТ его там, где успел: у каждой
записи есть возраст, и устаревшую никто не использует.
"""

import json
import os
import threading
import time

WS_URL = os.getenv('PM_WS_URL',
                   'wss://ws-subscriptions-clob.polymarket.com/ws/market')

# Насколько свежей должна быть запись, чтобы ею пользоваться. Поток присылает
# изменения сразу; молчание дольше этого срока означает, что по этому рынку
# ничего не происходит ЛИБО что соединение умерло, — и отличить одно от другого
# изнутри нельзя. Возвращаемся к опросу.
FRESH_SECONDS = float(os.getenv('PM_WS_FRESH_SECONDS', '60'))

_lock = threading.Lock()
_books = {}                 # token -> {'bids': [...], 'asks': [...], 'at': ts}
_state = {'running': False, 'connected': False, 'since': None,
          'messages': 0, 'last_error': '', 'tokens': 0, 'reconnects': 0}
_wanted = set()             # какие токены должны быть в подписке
_thread = None
_resubscribe = threading.Event()


def status():
    """Состояние подписки для панели и журнала."""
    with _lock:
        out = dict(_state)
        out['books'] = len(_books)
        fresh = sum(1 for b in _books.values()
                    if b.get('synced') and time.time() - b['at'] < FRESH_SECONDS)
        out['fresh'] = fresh
    return out


def top(token):
    """
    Лучшие цены по потоку либо None, если данных нет или они устарели.

    Возвращает ту же форму, что и book.top: с ней вызывающий код не должен
    знать, откуда пришли числа.
    """
    with _lock:
        row = _books.get(str(token))
        if not row or not row.get('synced'):
            # ДЕЛЬТЫ БЕЗ СНИМКА — НЕ КНИГА. Изменения уровней приходят и до
            # первого снимка, и по ним собирается огрызок из двух-трёх цен.
            # Лучшая цена по такому огрызку выдумана: наблюдалось «край 0.119»
            # там, где спред рынка полтора цента, и одиннадцать котировок из
            # двадцати пяти оказались без края вовсе.
            return None
        if time.time() - row['at'] > FRESH_SECONDS:
            return None
        bids, asks = row.get('bids') or [], row.get('asks') or []
    if not bids or not asks:
        return None
    bid = max(p for p, _ in bids)
    ask = min(p for p, _ in asks)
    if bid >= ask:
        return None                     # книга перекрещена — верить нельзя
    bid_size = sum(s for p, s in bids if abs(p - bid) < 1e-12)
    ask_size = sum(s for p, s in asks if abs(p - ask) < 1e-12)
    return {'bid': bid, 'ask': ask, 'mid': (bid + ask) / 2,
            'spread': ask - bid, 'bid_size': bid_size, 'ask_size': ask_size,
            'source': 'поток'}


def book(token):
    """Полный стакан по потоку либо None. Форма как у book.fetch_many."""
    with _lock:
        row = _books.get(str(token))
        if (not row or not row.get('synced')
                or time.time() - row['at'] > FRESH_SECONDS):
            return None
        return {'bids': list(row.get('bids') or []),
                'asks': list(row.get('asks') or [])}


def watch(tokens):
    """
    Задаёт, за какими рынками следить. Подписка обновляется на лету.

    Список рынков меняется при каждом пересмотре, и переподключаться ради
    этого нельзя: разрыв стоит секунд, а за секунды нас и подбирают.
    """
    global _wanted
    fresh = {str(t) for t in tokens or [] if t}
    with _lock:
        changed = fresh != _wanted
        _wanted = fresh
        _state['tokens'] = len(fresh)
    if changed:
        _resubscribe.set()
    return changed


def _apply_book(payload):
    """Полный снимок стакана: заменяет прежний целиком."""
    token = str(payload.get('asset_id') or '')
    if not token:
        return
    bids = [(float(x['price']), float(x['size']))
            for x in (payload.get('bids') or []) if x.get('price')]
    asks = [(float(x['price']), float(x['size']))
            for x in (payload.get('asks') or []) if x.get('price')]
    with _lock:
        # СНИМОК ДЕЛАЕТ КНИГУ ПРИГОДНОЙ. До него у нас в лучшем случае
        # несколько уровней из дельт, и «лучшая цена» по ним — выдумка.
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)
        _books[token] = {
            'bids': bids, 'asks': asks, 'at': time.time(), 'synced': True,
            # Размеры верхушки запоминаются отдельно: дельты приносят цены, но
            # не глубину, а очередь считается именно по ней.
            'bid_size': sum(s for p, s in bids
                            if best_bid is not None and abs(p - best_bid) < 1e-12),
            'ask_size': sum(s for p, s in asks
                            if best_ask is not None and abs(p - best_ask) < 1e-12)}


def _apply_change(payload):
    """
    Изменение стакана. Берём ГОТОВУЮ верхушку, а не пересобираем книгу.

    ЗДЕСЬ БЫЛА ОШИБКА, СТОИВШАЯ СЕМНАДЦАТИ БЕСПОЛЕЗНЫХ ЗАЯВОК. Прежняя версия
    вела полную книгу: находила уровень по цене, заменяла размер, убирала при
    нуле. Дельты приходят по ОДНОМУ уровню — и часто по глубокому, далёкому от
    верхушки: в захваченном сообщении цена изменения 0.162 при лучшей цене 0.2.
    Собранная так книга вырождалась в 0.001/0.999, и котировки по ней уходили
    на биржу покупками по цене в тысячную долю при рынке 0.926.

    А между тем КАЖДОЕ сообщение несёт `best_bid` и `best_ask` — ровно то, из
    чего считается край. Их и берём: верхушка приходит от биржи готовой, и
    ошибиться в ней нельзя.

    Глубина уровней остаётся за опросом. Она нужна только для очереди, меняется
    медленно, и ради неё держать хрупкий пересчёт не стоит.
    """
    for change in (payload.get('price_changes') or []):
        token = str(change.get('asset_id') or '')
        if not token:
            continue
        try:
            bid = float(change['best_bid'])
            ask = float(change['best_ask'])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 < bid < ask < 1:
            continue                    # верхушка бессмысленна — пропускаем
        with _lock:
            row = _books.setdefault(token, {'bids': [], 'asks': [],
                                            'at': time.time(),
                                            'synced': False})
            row['bids'] = [(bid, row.get('bid_size') or 0.0)]
            row['asks'] = [(ask, row.get('ask_size') or 0.0)]
            row['at'] = time.time()
            row['synced'] = True        # верхушка от биржи, собирать нечего


async def _run():
    import aiohttp

    while _state['running']:
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_read=90)
            # СИСТЕМНЫЙ РЕЗОЛВЕР ЗАДАЁТСЯ ЯВНО, и это не перестраховка.
            #
            # При установленном aiodns библиотека ходит к DNS-серверам сама,
            # мимо настроек системы. В сети с корпоративным или провайдерским
            # DNS это кончается «Could not contact DNS servers» — при том что
            # тот же адрес прекрасно разрешается обычным способом. Поймано
            # здесь же: REST-адреса работали, а адрес потока не разрешался
            # ни разу за минуту попыток.
            connector = aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver(), ttl_dns_cache=300)
            async with aiohttp.ClientSession(timeout=timeout,
                                             connector=connector) as session:
                async with session.ws_connect(WS_URL, heartbeat=20) as ws:
                    with _lock:
                        _state['connected'] = True
                        _state['since'] = time.time()
                        _state['last_error'] = ''
                    await _pump(ws)
        except Exception as exc:                            # noqa: BLE001
            with _lock:
                _state['last_error'] = f'{type(exc).__name__}: {str(exc)[:120]}'
        with _lock:
            _state['connected'] = False
            _state['reconnects'] += 1
        if not _state['running']:
            break
        # Пауза перед повтором. Без неё разрыв на стороне площадки превратился
        # бы в шторм переподключений — и в бан по лимиту.
        await _sleep(3)


async def _sleep(seconds):
    import asyncio
    await asyncio.sleep(seconds)


async def _pump(ws):
    import asyncio

    import aiohttp

    sent = set()
    while _state['running']:
        with _lock:
            want = set(_wanted)
        if want != sent and want:
            await ws.send_json({'assets_ids': sorted(want), 'type': 'market',
                                'initial_dump': True, 'level': 2})
            sent = set(want)
        _resubscribe.clear()

        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
        except asyncio.TimeoutError:
            continue                    # тишина — не беда, ждём дальше
        if msg.type != aiohttp.WSMsgType.TEXT:
            if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return
            continue
        try:
            data = json.loads(msg.data)
        except ValueError:
            continue
        rows = data if isinstance(data, list) else [data]
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = row.get('event_type')
            if kind == 'book':
                _apply_book(row)
            elif kind == 'price_change':
                _apply_change(row)
        with _lock:
            _state['messages'] += len(rows)


def start(tokens=None):
    """
    Поднимает подписку фоновым потоком. Повторный вызов лишь обновляет список.

    Возвращает False, если поток недоступен — вызывающий код обязан продолжить
    работать на опросе, а не останавливаться.
    """
    global _thread
    if tokens:
        watch(tokens)
    if _thread is not None and _thread.is_alive():
        return True
    try:
        import aiohttp                                      # noqa: F401
    except Exception as exc:                                # noqa: BLE001
        with _lock:
            _state['last_error'] = f'нет библиотеки для потока: {exc}'
        return False

    def spin():
        import asyncio
        asyncio.new_event_loop().run_until_complete(_run())

    _state['running'] = True
    _thread = threading.Thread(target=spin, daemon=True, name='pm-stream')
    _thread.start()
    return True


def stop():
    _state['running'] = False
    _resubscribe.set()
    return True
