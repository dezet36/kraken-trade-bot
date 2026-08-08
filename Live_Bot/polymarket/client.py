"""
Доступ к Polymarket: список рынков, стакан, история цен.

В ЭТОТ МОДУЛЬ ВСТРОЕНЫ ЛОВУШКИ, НАЙДЕННЫЕ ЗАМЕРАМИ, И КАЖДАЯ СТОИЛА ПРОГОНА.
Обходить их приходится всем, кто читает эту площадку, и повторять чужие
открытия по второму разу незачем.

    1. `closed=true` с сортировкой по УБЫВАНИЮ даты выдаёт рынки-заглушки с
       формальным концом в 2027 году: они закрыты досрочно и торгов в них не
       было. Первый сбор так добыл 1256 рынков, из которых 100% имели дату в
       будущем и 95% не имели истории цен вовсе. Лечится `end_date_max`.

    2. Смещение упирается в предел около двух тысяч, дальше 422. Шагать надо
       ПО ДАТЕ: сдвигать верхнюю границу к самому старому найденному рынку и
       обнулять смещение. Тот же приём понадобился для истории открытого
       интереса на бирже, где `since` не действовал.

    3. Отказ сети и пустой ответ выглядят одинаково. Записав первое в кэш, мы
       навсегда объявили бы рынок «без истории»; в первом сборе так пропало
       94% файлов. Пустой ответ здесь НЕ КЭШИРУЕТСЯ.

    4. Поиск погодных рынков по слову в заголовке ловит «Ukraine» на подстроку
       «rain». Признак берётся у самой биржи: feeType.
"""

import json
import time
import urllib.parse
import urllib.request

from . import params

_UA = {'User-Agent': 'Mozilla/5.0 (research bot)'}


def _get(url, as_json=True):
    """Запрос с повторами. None — не получилось, и это НЕ значит «пусто»."""
    for attempt in range(params.RETRIES):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=params.TIMEOUT) as resp:
                body = resp.read().decode('utf-8', 'replace')
            return json.loads(body) if as_json else body
        except Exception:                                   # noqa: BLE001
            if attempt == params.RETRIES - 1:
                return None
            time.sleep(1.0 + attempt)
    return None


def active_markets(limit_pages=20, fee_type=None):
    """Активные рынки, по убыванию оборота. fee_type — фильтр категории."""
    out = []
    for page in range(limit_pages):
        rows = _get(f'{params.GAMMA}/markets?limit=100&offset={page * 100}'
                    '&closed=false&order=volume&ascending=false')
        if not isinstance(rows, list) or not rows:
            break
        for m in rows:
            if fee_type and m.get('feeType') != fee_type:
                continue
            out.append(m)
        time.sleep(params.PAUSE)
    return out


def resolved_markets(want=2000, fee_type=None):
    """
    Разрешённые рынки, от свежих к старым, шагом ПО ДАТЕ.

    Возвращаются только чисто разрешённые: цены исходов ('1','0') либо
    ('0','1'). Прочие формы — отменённые и спорные рынки, исхода у них нет, и
    молча считать их проигрышем нельзя.
    """
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                           time.gmtime(time.time() - 12 * 3600))
    out, seen, offset = [], set(), 0
    while len(out) < want:
        rows = _get(f'{params.GAMMA}/markets?limit=100&offset={offset}'
                    f'&closed=true&end_date_max={urllib.parse.quote(cutoff)}'
                    '&order=endDate&ascending=false')
        if not isinstance(rows, list) or not rows or offset >= 1800:
            oldest = min((m.get('endDate') or '' for m in (rows or [])),
                         default='')
            if not oldest or oldest >= cutoff:
                break
            cutoff, offset = oldest, 0
            continue
        for m in rows:
            key = m.get('id')
            if key in seen:
                continue
            seen.add(key)
            if fee_type and m.get('feeType') != fee_type:
                continue
            try:
                prices = json.loads(m.get('outcomePrices') or '[]')
            except Exception:                              # noqa: BLE001
                continue
            if sorted(prices) != ['0', '1']:
                continue
            out.append(m)
        offset += 100
        time.sleep(params.PAUSE)
    return out


def event_by_slug(slug):
    """Событие целиком со всеми корзинами. None — не найдено."""
    rows = _get(f'{params.GAMMA}/events?slug={urllib.parse.quote(slug)}')
    return rows[0] if isinstance(rows, list) and rows else None


def order_book(token_id):
    """Стакан по токену. Возвращает (лучший бид, лучший аск, глубина)."""
    book = _get(f'{params.CLOB}/book?token_id={token_id}')
    if not book:
        return None
    bids = book.get('bids') or []
    asks = book.get('asks') or []
    if not bids or not asks:
        return None
    # У этой площадки заявки идут от худшей к лучшей: лучшая — ПОСЛЕДНЯЯ.
    # Взяв первую, мы получили бы самую далёкую цену и решили бы, что спред
    # огромен.
    best_bid = float(bids[-1]['price'])
    best_ask = float(asks[-1]['price'])
    depth = sum(float(b['size']) * float(b['price']) for b in bids)
    return {'bid': best_bid, 'ask': best_ask, 'spread': best_ask - best_bid,
            'depth_usd': depth}


def price_history(token_id, fidelity=60):
    """История цен токена. Пустой список — истории нет; None — не дозвонились."""
    data = _get(f'{params.CLOB}/prices-history?market={token_id}'
                f'&interval=max&fidelity={fidelity}')
    if data is None:
        return None
    return data.get('history') or []


def fee_rate(market):
    """Ставка комиссии рынка: из его расписания, иначе по категории."""
    schedule = market.get('feeSchedule') or {}
    rate = schedule.get('rate')
    if rate is not None:
        try:
            return float(rate)
        except (TypeError, ValueError):
            pass
    return params.FEE_RATES.get(market.get('feeType'), params.FEE_DEFAULT)


def entry_cost(price, rate):
    """
    Издержки входа В ДОЛЯХ ВЛОЖЕННОГО.

    Комиссия платится только на входе и считается от p × (1 - p), поэтому в
    процентах ставки она максимальна у дешёвых корзин: при цене 0.05 это 4.75%,
    при 0.95 — 0.26%. Пересечение спреда добавляется всегда.
    """
    if price <= 0:
        return float('inf')
    return (rate * price * (1 - price) + params.CROSS_COST) / price
