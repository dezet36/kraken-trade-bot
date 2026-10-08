"""
Реестр источников данных — модуль «Данные» (реорганизация, этап 3, 08.10.2026).

ОТКУДА БОТ БЕРЁТ ИНФОРМАЦИЮ ДЛЯ ТОРГОВЛИ — в одном месте: адрес, запасные
адреса, прокси (IP), предел ожидания, что источник даёт и кто его читает.
Только ЧТЕНИЕ рынка: торговые ключи и заявки сюда не относятся (торговый клиент
exchange.get_exchange — дело реальных счетов, этап 7).

АДРЕСА ПО УМОЛЧАНИЮ — те, с которыми бот работал до реестра. Оператор меняет
их без правки кода — файлом data_sources.json в каталоге данных (на сервере
/opt/kraken/bot_data/data_sources.json):

    {"binance_futures": {"url": "https://fapi1.binance.com",
                         "fallbacks": ["https://fapi2.binance.com"],
                         "proxy": "http://10.0.0.5:3128", "timeout": 30}}

Правка действует со следующего запроса (у рыночного клиента ccxt и потоков —
после перезапуска бота: клиент и соединение создаются один раз). С панели
адреса не меняются намеренно: у панели нет пароля, а подменённый адрес — это
подменённые котировки.

ЗДОРОВЬЕ. Каждый запрос через get() и через рыночный клиент ccxt
(instrument_ccxt) отмечает у источника успех или отказ: время, задержку, текст
ошибки. Потоки (WebSocket) отмечают себя сами (mark_ok / mark_fail). Панель
показывает это таблицей (snapshot(), /api/sources). Отказ источника не
останавливает торговлю — это прочерк в разметке и строка в журнале (CLAUDE.md,
«Данные»).
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from logger import log

FILE_NAME = 'data_sources.json'
USER_AGENT = 'kraken-bot/1.0'
TIMEOUT_S = 20

# Что даёт каждый источник и кто его читает — то, что видно на панели. Поля
# url/fallbacks/proxy/timeout меняются файлом; остальное — описание.
DEFAULTS = {
    'bybit': {
        'title': 'Bybit — рынок', 'kind': 'rest', 'url': 'https://api.bybit.com',
        # ccxt собирает адреса Bybit из шаблона с этим началом.
        'ccxt_prefix': 'https://api.{hostname}',
        'provides': ('свечи всех таймфреймов', 'фандинг', 'открытый интерес',
                     'доли счетов в лонге/шорте', 'премия к индексу', 'снимки ленты и стакана',
                     'тикеры спота', 'объявления биржи'),
        'readers': ('сканеры стратегий и брокер (свечи)', 'positioning', 'flow_data',
                    'market_cap', 'news_feed'),
        'limit': 'в первые секунды часа бывает «Too many visits» (10006) — повтор через 2/4/8 с',
    },
    'bybit_ws': {
        'title': 'Bybit — потоки', 'kind': 'ws',
        'url': os.getenv('BYBIT_WS_PUBLIC', 'wss://stream.bybit.com/v5/public/linear'),
        'provides': ('лента сделок', 'ликвидации'),
        'readers': ('trades_ws', 'liquidations'),
        'limit': 'не больше 10 тем в одной подписке; пинг каждые 20 с',
    },
    'bingx': {
        'title': 'BingX — рынок', 'kind': 'rest', 'url': 'https://open-api.bingx.com',
        'ccxt_prefix': 'https://open-api.{hostname}',
        'provides': ('свечи', 'фандинг'),
        'readers': ('сканеры стратегий и брокер, если выбрана BingX',),
        'limit': '',
    },
    'binance_futures': {
        'title': 'Binance — фьючерсы', 'kind': 'rest', 'url': 'https://fapi.binance.com',
        'provides': ('часовые свечи с долей покупок (поток)', 'объём фьючерса BTC'),
        'readers': ('flow_data', 'market_mood'),
        'limit': '',
    },
    'binance_spot': {
        'title': 'Binance — спот', 'kind': 'rest', 'url': 'https://api.binance.com',
        'provides': ('часовые свечи BTC спота',),
        'readers': ('market_mood',),
        'limit': '',
    },
    'deribit': {
        'title': 'Deribit', 'kind': 'rest', 'url': 'https://www.deribit.com',
        'provides': ('DVOL — индекс волатильности BTC',),
        'readers': ('market_mood',),
        'limit': '',
    },
    'coinbase': {
        'title': 'Coinbase', 'kind': 'rest', 'url': 'https://api.exchange.coinbase.com',
        'provides': ('часовые свечи BTC-USD (премия Coinbase)',),
        'readers': ('market_mood',),
        'limit': '',
    },
    'coingecko': {
        'title': 'CoinGecko', 'kind': 'rest', 'url': 'https://api.coingecko.com',
        'provides': ('предложение монет (капитализация рынка)',),
        'readers': ('market_cap',),
        'limit': 'без ключа — несколько запросов в минуту; бот спрашивает раз в час',
    },
}

_lock = threading.Lock()
_file = {'key': None, 'data': {}}
_health = {}


# ── Настройки источников ────────────────────────────────────────────────────

def settings_path():
    # config — заново: тесты перезагружают его, и каталог данных, схваченный
    # при импорте, был бы чужим.
    import config
    return os.path.join(config.DATA_DIR, FILE_NAME)


def _overrides():
    """Правки оператора из data_sources.json; {} — файла нет или он нечитаем."""
    path = settings_path()
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return {}
    with _lock:
        if _file['key'] == (path, stamp):
            return _file['data']
        try:
            with open(path, encoding='utf-8') as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError('ожидался словарь источников')
        except (OSError, ValueError) as exc:
            log(f'⚠️ {FILE_NAME} нечитаем ({exc}) — источники по адресам по умолчанию')
            data = {}
        _file['key'], _file['data'] = (path, stamp), data
        return data


def source(code):
    """Действующие настройки источника: умолчания + правка оператора."""
    base = DEFAULTS[code]
    own = _overrides().get(code)
    own = own if isinstance(own, dict) else {}
    out = {'code': code, **base,
           'url': str(own.get('url') or base['url']).rstrip('/'),
           'fallbacks': tuple(str(u).rstrip('/') for u in (own.get('fallbacks') or ()) if u),
           'proxy': str(own.get('proxy') or '') or None,
           'timeout': TIMEOUT_S, 'overridden': bool(own)}
    try:
        if own.get('timeout') is not None:
            out['timeout'] = max(1.0, min(float(own['timeout']), 120.0))
    except (TypeError, ValueError):
        pass
    return out


def codes():
    return tuple(DEFAULTS)


def url(code):
    """Основной адрес источника."""
    return source(code)['url']


def urls(code):
    """Основной адрес и запасные — в порядке обращения."""
    src = source(code)
    return (src['url'],) + src['fallbacks']


def proxy(code):
    return source(code)['proxy']


# ── Запрос ──────────────────────────────────────────────────────────────────

def _http_json(full_url, timeout, proxy_url=None):
    request = urllib.request.Request(full_url, headers={'User-Agent': USER_AGENT})
    if proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url}))
        with opener.open(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _worth_another_address(exc):
    """К запасному адресу идём, когда не ответил сам адрес (сеть, таймаут,
    5xx, предел запросов), а не когда сервер ответил «такого нет» (4xx):
    запасной ответит тем же, а запросов станет вдвое больше."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    return True


def get(code, path, timeout=None):
    """
    JSON по пути у источника: основной адрес, при отказе — запасные по
    порядку. Отказ всех — исключение последнего (вызывающий решает, что это
    прочерк).
    """
    src = source(code)
    last = None
    for base in (src['url'],) + src['fallbacks']:
        started = time.monotonic()
        try:
            data = _http_json(base + path, timeout or src['timeout'], src['proxy'])
        except Exception as exc:                       # noqa: BLE001
            mark_fail(code, exc, via=base)
            last = exc
            if not _worth_another_address(exc):
                break
            continue
        mark_ok(code, (time.monotonic() - started) * 1000, via=base)
        return data
    raise last


# ── Рыночный клиент ccxt ────────────────────────────────────────────────────

def instrument_ccxt(client, code):
    """
    Рыночный клиент ccxt под реестр: адрес и прокси — из настроек источника,
    каждый запрос отмечает здоровье. Без правок оператора адреса не трогаются.
    """
    src = source(code)
    prefix = src.get('ccxt_prefix')
    if prefix and src['url'] != DEFAULTS[code]['url']:
        def swap(value):
            if isinstance(value, str):
                return value.replace(prefix, src['url'])
            if isinstance(value, dict):
                return {k: swap(v) for k, v in value.items()}
            return value
        client.urls['api'] = swap(client.urls['api'])
    if src['proxy']:
        client.httpsProxy = src['proxy']

    raw_fetch = client.fetch

    def fetch(target, method='GET', headers=None, body=None):
        started = time.monotonic()
        try:
            out = raw_fetch(target, method, headers, body)
        except Exception as exc:
            mark_fail(code, exc)
            raise
        mark_ok(code, (time.monotonic() - started) * 1000)
        return out

    client.fetch = fetch
    return client


# ── Здоровье ────────────────────────────────────────────────────────────────

def _record(code):
    return _health.setdefault(code, {'ok': 0, 'fail': 0, 'last_ok': None, 'last_fail': None,
                                     'error': '', 'ms': None, 'via': ''})


def mark_ok(code, ms=None, via=''):
    with _lock:
        h = _record(code)
        h['ok'] += 1
        h['last_ok'] = time.time()
        if ms is not None:
            h['ms'] = round(float(ms), 1)
        if via:
            h['via'] = via


def mark_fail(code, exc, via=''):
    with _lock:
        h = _record(code)
        h['fail'] += 1
        h['last_fail'] = time.time()
        h['error'] = str(exc)[:200]
        if via:
            h['via'] = via


def _mask(value):
    """Логин и пароль в адресе прокси на панель не идут."""
    return re.sub(r'//[^/@]+@', '//***@', str(value)) if value else value


def snapshot():
    """Таблица для панели: адреса (без паролей), что даёт, кто читает, здоровье."""
    rows = []
    with _lock:
        health = {k: dict(v) for k, v in _health.items()}
    for code in DEFAULTS:
        src = source(code)
        h = health.get(code, {})
        last_ok, last_fail = h.get('last_ok'), h.get('last_fail')
        if not last_ok and not last_fail:
            status = 'нет запросов'
        elif (last_fail or 0) > (last_ok or 0):
            status = 'сбой'
        else:
            status = 'отвечает'
        rows.append({
            'code': code, 'title': src['title'], 'kind': src['kind'],
            'url': _mask(src['url']), 'fallbacks': [_mask(u) for u in src['fallbacks']],
            'proxy': _mask(src['proxy']) or '', 'timeout': src['timeout'],
            'overridden': src['overridden'],
            'provides': list(src['provides']), 'readers': list(src['readers']),
            'limit': src['limit'], 'status': status,
            'last_ok': last_ok, 'last_fail': last_fail, 'error': h.get('error', ''),
            'ms': h.get('ms'), 'ok': h.get('ok', 0), 'fail': h.get('fail', 0),
            'via': _mask(h.get('via', '')),
        })
    return {'sources': rows, 'file': settings_path()}
