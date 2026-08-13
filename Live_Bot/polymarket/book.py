"""
Стакан заявок и модель очереди. Ядро маркет-мейкинга.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ЧАСТЬ КЛИЕНТА. Клиент отвечает на вопрос
«что сейчас на бирже». Здесь — «что было бы с НАШЕЙ заявкой», а это совсем
другое: тут живёт единственная величина, которой нет ни в одной истории цен, и
ради которой затевается вся Фаза 0.

ГЛАВНОЕ, ЧТО НАДО ПОНИМАТЬ ПРО ЭТУ СТРАТЕГИЮ. Направленную стратегию можно
проверить на истории: цены известны, сделка либо дошла до цели, либо нет.
Маркет-мейкинг проверить так НЕЛЬЗЯ, и это не придирка, а определяющее
ограничение. Он живёт двумя величинами, которых в истории цен не существует:

    1. ИСПОЛНИЛАСЬ БЫ наша заявка. История хранит сделки, а не очередь. Стояли
       мы в ней первыми или двадцатыми — из ленты не восстановить.
    2. КТО КОГО СНЯЛ. Нас исполняют тогда, когда встречной стороне это выгодно.
       На дешёвых контрактах особенно больно: заявку на продажу по 0.06 снимают
       ровно тогда, когда событие вдруг стало вероятным.

Отсюда весь замысел Фазы 0: котировать понарошку поверх ЖИВОГО стакана и
записывать, что происходило дальше. Это единственный источник обеих величин.

ПОРЯДОК ЗАЯВОК В ОТВЕТЕ БИРЖИ ОБРАТНЫЙ ОЖИДАЕМОМУ, И ЭТО ПРОВЕРЕНО ДВАЖДЫ.
Биды приходят по возрастанию цены, аски — по убыванию; лучшая заявка в обоих
случаях ПОСЛЕДНЯЯ. Взяв первую, мы получили бы самую далёкую цену и решили бы,
что спред огромен. Сверено с полем `spread` самого рынка: при бидах
0.001/0.002/0.003 и асках 0.006/0.005/0.004 биржа сообщает спред 0.001, то есть
считает лучшими 0.003 и 0.004.
"""

import json
import time
import urllib.request

from . import params

_UA = {'User-Agent': 'Mozilla/5.0 (research bot)'}


def _get(url):
    for attempt in range(params.RETRIES):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=params.TIMEOUT) as resp:
                return json.loads(resp.read().decode('utf-8', 'replace'))
        except Exception:                                   # noqa: BLE001
            if attempt == params.RETRIES - 1:
                return None
            time.sleep(0.5 + attempt)
    return None


def fetch(token_id):
    """
    Стакан, приведённый к виду «лучшая заявка первая».

    Возвращает {'bids': [(цена, размер), ...], 'asks': [...]} по убыванию
    выгодности. None — не дозвонились; пустые списки — стакана нет.
    """
    raw = _get(f'{params.CLOB}/book?token_id={token_id}')
    if raw is None:
        return None
    def side(rows, best_first_desc):
        out = []
        for row in rows or []:
            try:
                out.append((float(row['price']), float(row['size'])))
            except (KeyError, TypeError, ValueError):
                continue
        # Биржа отдаёт биды по возрастанию, аски по убыванию. Сортируем сами и
        # не полагаемся на порядок ответа: он уже один раз оказался обратным
        # ожидаемому, и молчаливая зависимость от него — приглашение к ошибке.
        return sorted(out, key=lambda x: x[0], reverse=best_first_desc)
    return {'bids': side(raw.get('bids'), True),
            'asks': side(raw.get('asks'), False)}


def top(book):
    """Лучшие цены, спред и середина. None там, где стороны нет."""
    if not book:
        return None
    bid = book['bids'][0][0] if book['bids'] else None
    ask = book['asks'][0][0] if book['asks'] else None
    out = {'bid': bid, 'ask': ask, 'spread': None, 'mid': None,
           'bid_size': book['bids'][0][1] if book['bids'] else 0.0,
           'ask_size': book['asks'][0][1] if book['asks'] else 0.0}
    if bid is not None and ask is not None:
        out['spread'] = round(ask - bid, 6)
        out['mid'] = round((ask + bid) / 2, 6)
    return out


def depth_ahead(book, side, price):
    """
    Сколько объёма стоит ПЕРЕД нашей заявкой по цене `price`.

    Считается всё, что исполнится раньше нас: заявки по более выгодной для
    встречной стороны цене плюс всё, что уже стоит на нашей цене. Заявки на
    нашей цене учитываются ЦЕЛИКОМ — мы встаём в конец очереди, а не в начало.
    Иначе модель обещала бы исполнения, которых не будет.

    side: 'bid' — мы покупаем, 'ask' — мы продаём.
    """
    if not book:
        return None
    if side == 'bid':
        # Нас исполнит агрессивная продажа. Раньше нас пострадают биды ВЫШЕ
        # нашего и всё, что стоит на нашей цене.
        return sum(size for p, size in book['bids'] if p > price + 1e-9) + \
               sum(size for p, size in book['bids'] if abs(p - price) < 1e-9)
    return sum(size for p, size in book['asks'] if p < price - 1e-9) + \
           sum(size for p, size in book['asks'] if abs(p - price) < 1e-9)


def quote(book, tick, step=1, min_size=None):
    """
    Куда встать двусторонней котировкой: на `step` тиков внутрь от лучших цен.

    step=0 означает «встать вровень с лучшей ценой», step=1 — на тик лучше,
    то есть перебить очередь. Возвращает None, если стакан односторонний:
    котировать вслепую там, где нет встречной цены, нельзя — не от чего
    считать ни спред, ни середину.

    ДВУСТОРОННОСТЬ ЗДЕСЬ НЕ ПРИХОТЬ. Награда за ликвидность при середине рынка
    вне диапазона 0.10-0.90 начисляется ТОЛЬКО за двусторонние заявки;
    односторонняя не даёт ничего. А поскольку самый большой перевес мейкера над
    тейкером как раз на дешёвых контрактах, работать придётся именно там.
    """
    info = top(book)
    if not info or info['bid'] is None or info['ask'] is None:
        return None
    bid = round(info['bid'] + step * tick, 6)
    ask = round(info['ask'] - step * tick, 6)
    if bid >= ask:
        # Спред уже, чем два наших шага: встать внутрь нельзя, получилась бы
        # заявка, пересекающая рынок, то есть тейкерская — с комиссией и без
        # награды. Возвращаемся к лучшим ценам.
        bid, ask = info['bid'], info['ask']
    if bid <= 0 or ask >= 1:
        return None
    return {'bid': bid, 'ask': ask,
            'size': float(min_size or params.MM_QUOTE_SIZE),
            'spread': round(ask - bid, 6),
            'mid': info['mid']}


def rewards_eligible(quoted, market, book=None):
    """
    Попадает ли наша котировка под награду.

    Условий три, и все три взяты у самой биржи, а не выдуманы:
      * размер не меньше `rewardsMinSize` — иначе заявка не считается вовсе;
      * обе цены не дальше `rewardsMaxSpread` центов от середины;
      * при середине вне 0.10-0.90 котировка обязана быть ДВУСТОРОННЕЙ.

    Третье условие и делает эту стратегию двусторонней по существу: именно на
    дешёвых контрактах перевес мейкера наибольший, и именно там односторонняя
    заявка не приносит ничего.
    """
    if not quoted:
        return {'eligible': False, 'why': 'нет двусторонней котировки'}
    min_size = float(market.get('rewardsMinSize') or 0)
    max_spread_cents = float(market.get('rewardsMaxSpread') or 0)
    if max_spread_cents <= 0:
        return {'eligible': False, 'why': 'рынок без награды'}
    if quoted['size'] < min_size:
        return {'eligible': False,
                'why': f'размер {quoted["size"]:.0f} меньше {min_size:.0f}'}
    mid = quoted.get('mid')
    if mid is None:
        return {'eligible': False, 'why': 'середина не определена'}
    limit = max_spread_cents / 100.0
    far_bid = mid - quoted['bid']
    far_ask = quoted['ask'] - mid
    if far_bid > limit + 1e-9 or far_ask > limit + 1e-9:
        return {'eligible': False,
                'why': f'дальше {max_spread_cents:.1f} цента от середины'}
    return {'eligible': True, 'why': '', 'mid': mid,
            'two_sided_required': not 0.10 <= mid <= 0.90}


def tape(condition_id, limit=500):
    """Лента сделок рынка: цена, размер, сторона, время."""
    rows = _get(f'https://data-api.polymarket.com/trades?'
                f'market={condition_id}&limit={limit}')
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        try:
            out.append({'price': float(row['price']),
                        'size': float(row['size']),
                        'side': row.get('side'),
                        'asset': row.get('asset'),
                        'ts': int(row['timestamp'])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda r: r['ts'])


def would_fill(side, price, queue_ahead, trades, token_id=None):
    """
    Исполнилась бы наша заявка, судя по ленте.

    Модель осознанно ПЕССИМИСТИЧНАЯ, и это единственный честный выбор: любая
    поблажка здесь превращает замер в обещание. Приняты три допущения, каждое
    не в нашу пользу.

      1. Очередь перед нами не тает. На деле часть заявок снимают, и мы
         поднимались бы вверх быстрее. Считаем, что не снимают.
      2. Исполнить нас может только сделка по цене, ДОСТИГШЕЙ нашей: для
         покупки — прошедшая по нашей цене или ниже.
      3. Объём засчитывается только от встречной стороны: покупку исполняет
         агрессивная продажа, и наоборот.

    Возвращает словарь с признаком исполнения, временем и объёмом, который
    пришлось пропустить, либо None — если ленты нет.
    """
    if trades is None:
        return None
    need = float(queue_ahead or 0.0)
    seen = 0.0
    for t in trades:
        if token_id and t.get('asset') and t['asset'] != token_id:
            continue
        if side == 'bid':
            hit = t['price'] <= price + 1e-9 and t['side'] == 'SELL'
        else:
            hit = t['price'] >= price - 1e-9 and t['side'] == 'BUY'
        if not hit:
            continue
        seen += t['size']
        if seen > need:
            return {'filled': True, 'ts': t['ts'], 'consumed': seen,
                    'queue_ahead': need}
    return {'filled': False, 'ts': None, 'consumed': seen, 'queue_ahead': need}
