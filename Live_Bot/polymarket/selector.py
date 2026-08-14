"""
Отбор рынков: где стоять двусторонней котировкой и зарабатывать на спреде.

ЗАДАЧА ИМЕННО ТАКАЯ, а не «собрать награды». Разобранный кошелёк @planktonxd
делает 1 495 сделок в сутки по $1.79, входит мейкером (комиссии 0.13% против
4.7% у тейкера) и берёт 1.5 цента на контракте — это 16% от цены покупки. Он
работает в мелких событиях, где есть стакан, и живёт разницей цен, а не наградой.
Награда, если рынок под неё подходит, идёт сверх и в отборе не главная.

ТРИ РАЗА ЗА РАЗБОР ОДНА И ТА ЖЕ ЛОВУШКА, поэтому она здесь описана подробно.
Всякий раз, когда рынок отбирался по одному числу — награде, отношению награды
к ликвидности, относительному спреду, — наверх всплывали ПУСТЫЕ книги:

    награда к ликвидности      бидов на $2,  асков на $15,  спред 0.889
    относительный спред        бидов на $0,  асков на $11 849, спред 200%

Второй случай особенно поучителен. Дешёвый лонгшот НИКТО не хочет покупать по
два цента, зато многие хотят продать. Наш бид оказался бы единственным: нас
исполнят немедленно, и мы получим ровно тот хвост дешёвых позиций, на котором
разобранный кошелёк держит переоценку -$8 564 при 22% позиций в плюсе.

Отсюда правило: рынок отбирается по СТАКАНУ, а не по полю ликвидности, и обе
стороны должны быть живыми. Поле `liquidity` считает всё вместе и односторонний
стакан от двустороннего не отличает.

ЧТО ОСТАЁТСЯ ПОСЛЕ ОТСЕВА. Из 1 282 кандидатов книга есть у 600, глубина не
меньше $100 по обеим сторонам — у 410, без сильного перекоса — у 82. Медианный
спред у этих 82 составляет 6% от ставки за круг. Высокие проценты, которые
манили раньше, жили ровно в односторонних книгах.

РАЗМЕР ЗАЯВКИ — МИНИМАЛЬНЫЙ ДОПУСТИМЫЙ, ПЯТЬ КОНТРАКТОВ. Порог 20-200 относится
только к награде, а для заработка на спреде он не нужен. Двусторонняя котировка
в пять контрактов стоит РОВНО $5 при любой цене: покупка берёт 5p, продажа
5(1-p), в сумме пятёрка. Значит сотня долларов — это двадцать рынков, а не пять.
"""

import json
import time
from datetime import datetime, timezone

from . import book as book_mod
from . import client, params


def rewards_daily(market):
    return sum(float(r.get('rewardsDailyRate') or 0)
               for r in (market.get('clobRewards') or []))


def quote_cost(size, price):
    """
    Во что обходится двусторонняя котировка.

    Обе стороны требуют денег: покупка стоит p за контракт, продажа — (1-p),
    потому что продавать надо то, чего у нас нет. В сумме получается ровно
    размер, независимо от цены. Считать одну сторону значило бы вдвое занизить
    потребность и обнаружить это на первом отказе биржи.
    """
    return float(size) * float(price) + float(size) * (1 - float(price))


def _hours_left(end):
    """Часов до разрешения. Без даты возвращает бесконечность, а не ноль."""
    if not end:
        return float('inf')
    try:
        stamp = str(end).replace('Z', '+00:00')
        left = (datetime.fromisoformat(stamp)
                - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:                                      # noqa: BLE001
        return float('inf')
    return left


def _candidates(pages, min_volume, price_lo, price_hi):
    """Первичный отсев по метаданным. Стакан здесь ещё не смотрим."""
    rows = []
    for page in range(pages):
        chunk = client._get(f'{params.GAMMA}/markets?limit=100'
                            f'&offset={page * 100}&closed=false')
        if not isinstance(chunk, list) or not chunk:
            break
        for m in chunk:
            try:
                tokens = json.loads(m.get('clobTokenIds') or '[]')
                price = float(json.loads(m.get('outcomePrices') or '[]')[0])
            except Exception:                              # noqa: BLE001
                continue
            spread = float(m.get('spread') or 0)
            if not tokens or spread <= 0:
                continue
            if not price_lo < price < price_hi:
                continue
            if float(m.get('volume') or 0) < min_volume:
                continue
            # РЫНОК ПЕРЕД РАЗРЕШЕНИЕМ НЕ КОТИРУЕМ, и это самый крупный риск во
            # всей затее. Мейкер зарабатывает полтора цента на контракте; при
            # разрешении контракт становится нулём или единицей ЦЕЛИКОМ. Один
            # запас в пять контрактов по 0.20, застигнутый разрешением не в ту
            # сторону, стоит доллар — то есть тринадцать удачных кругов.
            #
            # Наклон против запаса разгружает нас за часы, а не за минуты, и
            # перед самым разрешением разгружать уже некому: ликвидность
            # уходит первой. Поэтому порог по времени, а не надежда на выход.
            if _hours_left(m.get('endDate')) < params.MM_MIN_HOURS_LEFT:
                continue
            rows.append({
                'id': m.get('id'), 'question': m.get('question'),
                'condition_id': m.get('conditionId'), 'token_id': tokens[0],
                'token_no': tokens[1] if len(tokens) > 1 else None,
                'tick': float(m.get('orderPriceMinTickSize') or 0.001),
                'order_min': float(m.get('orderMinSize') or 5),
                'rewards_daily': rewards_daily(m),
                'rewardsMinSize': m.get('rewardsMinSize'),
                'rewardsMaxSpread': m.get('rewardsMaxSpread'),
                'liquidity': float(m.get('liquidity') or 0),
                'volume': float(m.get('volume') or 0),
                'meta_price': price, 'meta_spread': spread,
                'end': m.get('endDate'), 'fee_type': m.get('feeType'),
                'event_id': ((m.get('events') or [{}])[0] or {}).get('id'),
            })
        time.sleep(params.PAUSE)
    return rows


def scan(budget=None, limit=None, pages=30, min_volume=None,
         min_depth=None, min_balance=None):
    """
    Рынки, где двусторонняя котировка имеет смысл. Лучшие первыми.

    Отсев идёт в два шага, и второй обязателен: сначала метаданные, потом
    НАСТОЯЩИЙ стакан. Пропустив второй, мы отобрали бы односторонние книги, где
    наша заявка окажется единственной со своей стороны и её снимут немедленно.
    """
    budget = float(budget if budget is not None else params.bankroll_for('MM'))
    limit = int(limit or params.MM_MARKETS)
    min_volume = float(min_volume if min_volume is not None
                       else params.MM_MIN_VOLUME)
    min_depth = float(min_depth if min_depth is not None
                      else params.MM_MIN_SIDE_DEPTH)
    min_balance = float(min_balance if min_balance is not None
                        else params.MM_MIN_BALANCE)

    rows = _candidates(pages, min_volume, params.MM_MIN_PRICE,
                       params.MM_MAX_PRICE)
    if not rows:
        return []

    books = book_mod.fetch_many([r['token_id'] for r in rows])
    good = []
    for row in rows:
        live = books.get(str(row['token_id']))
        if not live or not live['bids'] or not live['asks']:
            continue
        top = book_mod.top(live)
        if not top or top['mid'] is None or top['spread'] is None:
            continue
        # ГЛУБИНА СЧИТАЕТСЯ В ДЕНЬГАХ, А НЕ В КОНТРАКТАХ. Тысяча контрактов по
        # цене 0.002 — это два доллара, и называть такую сторону живой нельзя.
        bid_usd = sum(size * price for price, size in live['bids'])
        ask_usd = sum(size * (1 - price) for price, size in live['asks'])
        if bid_usd < min_depth or ask_usd < min_depth:
            continue
        # ПЕРЕКОС МЕРЯЕТСЯ В КОНТРАКТАХ, А НЕ В ДЕНЬГАХ. В деньгах он врёт по
        # построению: при РАВНОЙ глубине в контрактах бид на цене 0.90 стоит
        # $900, а аск — $100, и книга выглядит перекошенной девятикратно, хотя
        # она идеально симметрична. Так отбраковывался КАЖДЫЙ рынок дороже 0.80
        # и дешевле 0.20 — независимо от того, что в нём на самом деле.
        #
        # Глубина по-прежнему считается в деньгах: там вопрос «сколько стоит
        # эта сторона», и ответ на него денежный. Перекос спрашивает другое —
        # «одинаково ли готовы покупать и продавать», и это про контракты.
        bid_lots = sum(size for _, size in live['bids'])
        ask_lots = sum(size for _, size in live['asks'])
        balance = min(bid_lots, ask_lots) / max(bid_lots, ask_lots, 1e-9)
        if balance < min_balance:
            continue

        # СПРЕД ДОЛЖЕН ВМЕЩАТЬ ШАГ ВНУТРЬ. Условие здесь ТО ЖЕ, что в стратегии,
        # и это не дублирование, а согласование: без него отбор обещал 90
        # рынков и $450 вложений, а котировалось 22 — на остальных стратегия
        # отказывалась вставать в конец очереди, и план расходился с делом.
        #
        # Узкий спред не изъян рынка, а отсутствие места для нас: при спреде в
        # один тик заработать нечего, а встать можно только за очередь из 152
        # контрактов по медиане.
        # ОДНОТИКОВЫЕ РЫНКИ БОЛЬШЕ НЕ ОТБРАСЫВАЮТСЯ ЗДЕСЬ. Прежде отсев шёл по
        # ширине спреда, и это была ошибка: замер по 53 рынкам показал, что у
        # шести ожидание меньше часа, медиана 23 минуты, и почти все они как
        # раз однотиковые. Спред узок там потому, что торгуют. Решение теперь
        # принимает время ожидания — ниже, после замера потока.
        ticks = int(round(top['spread'] / row['tick'])) if row['tick'] > 0 else 0

        size = max(row['order_min'], params.MM_MIN_ORDER_SIZE)
        cost = quote_cost(size, top['mid'])
        if cost <= 0 or cost > budget:
            continue

        row.update({
            'price': top['mid'], 'spread': top['spread'],
            'bid_usd': round(bid_usd, 2), 'ask_usd': round(ask_usd, 2),
            'balance': round(balance, 3), 'size': size, 'cost': round(cost, 2),
            # Спред в долях цены. Это НЕ ожидаемый доход, и называть его так
            # было бы обманом: полный спред достаётся лишь тому, у кого
            # исполнились ОБЕ стороны, а широкий спред как раз и означает, что
            # никто не хочет котировать теснее. Обе наши заявки в таком рынке
            # исполнятся только тогда, когда кому-то станет ясно, куда идёт
            # цена, — то есть против нас.
            'spread_share': round(top['spread'] / max(top['mid'], 1e-9), 4),
        })
        good.append(row)

    # НИЖНЕГО ПОРОГА ПО ДОЛЕ СПРЕДА БОЛЬШЕ НЕТ. Он отбраковывал дорогие рынки:
    # спред в восемь тиков при цене 0.80 составляет всего 1% от неё, и рынок с
    # ожиданием в СЕМЬ МИНУТ и доходом $0.25 в час отсеивался как «узкий».
    # Достаточно требовать, чтобы после нашего шага что-то оставалось, — это
    # проверяет our_gain ниже.
    #
    # Верхний порог остаётся: слишком широкий спред означает, что рынок не
    # знает цены, и исполнят нас там ровно тогда, когда узнает.
    good = [r for r in good if r['spread_share'] <= params.MM_MAX_SPREAD_SHARE]
    good = measure_wait(good, books)
    # ОТСЕВ ПО ВРЕМЕНИ, А НЕ ПО ШИРИНЕ СПРЕДА. Рынок, где круг занимает сутки,
    # бесполезен при любом спреде: деньги стоят, а разрешение приближается.
    good = [r for r in good
            if r['wait_hours'] <= params.MM_MAX_WAIT_HOURS
            and r['our_gain'] > 0]

    # ПОРЯДОК ПО ДЕНЬГАМ В ЧАС, а не по обороту и не по ширине спреда.
    #
    # Оборот обманывает: он накоплен за всю жизнь рынка. Отобранные по нему
    # рынки имели по $152 000 оборота и торговали 0.3 раза в час — семь сделок
    # в сутки на рынок. При таком темпе безразлично, насколько хорошо мы
    # котируем: круг просто не с кем совершить.
    #
    # Ширина спреда обманывает иначе: самые быстрые рынки биржи (20+ сделок в
    # час) имеют спред в ОДИН тик. Там нечего брать, и это не жадность рынка, а
    # его правильная работа — где многим нужна немедленность, конкуренция
    # сжимает спред. Широкий спред означает ровно обратное: немедленность
    # никому не нужна.
    #
    # Заработок — произведение того и другого. Меряем частоту по ЛЕНТЕ, а не
    # по обороту, и ставим наверх рынки с наибольшим ожидаемым доходом в час.
    good.sort(key=lambda r: -r['usd_per_hour'])
    good = _cap_per_event(good)[:limit]
    # РАЗМЕР ПО ПОТОКУ, А НЕ ПЯТЁРКА. Рынков после отсева остаётся немного, и с
    # жёсткой пятёркой из сотни долларов работали тридцать: остальное стояло.
    # Поток мерится в контрактах в час; берём его долю, чтобы заявка успевала
    # исполниться, а не висела остатком.
    for row in good:
        row['size'] = size_for(row, budget)
        row['cost'] = round(quote_cost(row['size'], row['price']), 2)
        # ВРЕМЯ ПЕРЕСЧИТЫВАЕТСЯ ПОД НОВЫЙ РАЗМЕР. Пойманная на себе ошибка:
        # размер поднимался с 5 до 34 контрактов, а ожидание оставалось от
        # пятёрки — и доход выходил завышенным ровно во столько же раз.
        # Большую заявку рынку надо дольше выбирать; иначе выходило, что
        # тридцать четыре контракта исполняются за две минуты при потоке в
        # шестьдесят восемь контрактов в час.
        _recompute_wait(row)
    return good


def _step_options(ticks):
    """
    Возможные шаги внутрь спреда, в тиках. Ноль означает встать на лучшую цену.

    Шаг должен оставлять хотя бы тик: при спреде в шесть тиков глубже двух не
    уйти, иначе цены сойдутся и заявка станет тейкерской.
    """
    room = (ticks - 1) // 2
    return [0] + [d for d in (1, 2, 3, 5, 8, 12) if d <= room]


def _recompute_wait(row):
    """
    Ожидание и доход под окончательный размер заявки.

    ОБЕ ЗАЯВКИ СТОЯТ ОДНОВРЕМЕННО, и это меняет арифметику. Прежде ожидания
    складывались, как будто мы сперва покупаем, потом выставляем продажу, —
    но бид и аск живут в стакане вместе, и часы у них идут параллельно.

    Для независимых ожиданий со средними a и b среднее время, когда случатся
    ОБА, равно a + b - ab/(a+b). Это всегда меньше суммы и всегда больше
    большего из двух: сложение завышало ожидание в полтора раза при равных
    сторонах, то есть занижало доход настолько же.
    """
    size = float(row['size'])
    flow_in = float(row.get('flow_in') or 0)
    flow_out = float(row.get('flow_out') or 0)
    wait_in = ((row.get('queue_in', 0) + size) / flow_in
               if flow_in > 0 else float('inf'))
    wait_out = ((row.get('queue_out', 0) + size) / flow_out
                if flow_out > 0 else float('inf'))
    if wait_in == float('inf') or wait_out == float('inf'):
        both = float('inf')
    else:
        total = wait_in + wait_out
        both = total - (wait_in * wait_out / total) if total > 0 else 0.0
    row['wait_hours'] = round(both, 3)
    row['usd_per_hour'] = round(size * row['our_gain'] / row['wait_hours'], 5)         if row['wait_hours'] not in (0, float('inf')) else 0.0
    return row


def size_for(row, budget):
    """
    Размер заявки от ПОТОКА рынка, с тремя потолками.

    Пятёрка на всё подряд оставляла деньги без дела: после отсева по времени
    остаётся горстка рынков, и на них уходило $30 из ста. Поток при этом
    измерен и известен — по нему и считаем.

    ТРИ ПОТОЛКА, КАЖДЫЙ ОТ СВОЕЙ БЕДЫ. Доля потока: больше, чем рынок
    пропускает, взять нельзя — остаток заявки простоит и исказит наш же замер
    ожидания. Доля счёта: один рынок не должен решать судьбу счёта. Доля
    стакана: быть большей частью книги значит двигать цену собой.
    """
    flow = min(float(row.get('flow_in') or 0), float(row.get('flow_out') or 0))
    by_flow = flow * params.MM_FLOW_HOURS
    by_money = budget * params.MM_MAX_MARKET_SHARE
    by_book = min(float(row.get('bid_usd') or 0),
                  float(row.get('ask_usd') or 0)) * params.MM_MAX_BOOK_SHARE
    size = min(by_flow, by_money, by_book)
    floor = max(float(row.get('order_min') or 0), params.MM_MIN_ORDER_SIZE)
    return floor if size < floor else float(int(size))


def as_yes(trades, token_yes, token_no):
    """
    Сделки ОБОИХ токенов, приведённые к нашей стороне.

    ЗДЕСЬ ПРЯТАЛАСЬ САМАЯ КРУПНАЯ ОШИБКА ЗАМЕРА. У бинарного рынка два токена,
    и покупка «нет» по цене q экономически есть продажа «да» по цене (1-q).
    Считая сделки только своего токена, мы видели меньшую часть потока:

        «Will Nigel Farage win at least 80%» — 25 продаж «да» в нашем счёте
        против 363 настоящих, потому что 338 покупок «нет» мы не считали вовсе.

    Из-за этого 276 рынков из 327 объявлялись «без потока с одной стороны» и
    выбрасывались. Отбор считал мёртвыми ровно те рынки, где торговля шла
    через встречный токен.

    Цена зеркалится, сторона переворачивается. Размер остаётся: контракт «нет»
    и контракт «да» — одна и та же единица.
    """
    out = []
    for trade in trades or []:
        asset = trade.get('asset')
        if asset == token_yes:
            out.append(trade)
        elif token_no and asset == token_no:
            out.append({**trade,
                        'price': round(1.0 - float(trade['price']), 10),
                        'side': 'SELL' if trade['side'] == 'BUY' else 'BUY'})
    return out


def depth_at(book, side, price):
    """Контрактов на этой самой цене — очередь, если встать рядом."""
    levels = book['bids'] if side == 'bid' else book['asks']
    return sum(size for lvl, size in levels if abs(lvl - price) < 1e-9)


def measure_wait(rows, books, limit=None):
    """
    Сколько ЖДАТЬ исполнения — вот что решает, а не ширина спреда.

    СОБСТВЕННАЯ ОШИБКА, ИСПРАВЛЕННАЯ ЗАМЕРОМ. Рынки со спредом в один тик
    отбрасывались на том основании, что впереди стоит 152 контракта по медиане.
    Длина очереди мерилась на МЕДЛЕННЫХ рынках и была перенесена на все.

    Очередь важна не длиной, а временем. Замер по 53 рынкам: у шести ожидание
    меньше часа, медиана 23 минуты, и почти все они — как раз однотиковые.
    «Will Alexandria Ocasio-Cortez win...»: очередь 9 контрактов при потоке 530
    в час, то есть ОДНА МИНУТА. Такой рынок мы выбрасывали.

    Спред узок там именно потому, что торгуют, — и там же очередь рассасывается.

        время до исполнения = (очередь + наш размер) / поток

    Формула одна на оба случая. Шагнув внутрь, мы создаём новый уровень, где
    очередь равна нулю, и ждём только собственный размер. Встав на лучшую цену,
    ждём ещё и всех, кто пришёл раньше.
    """
    day = 24 * 3600
    now = time.time()
    # ЛЕНТЫ БЕРУТСЯ РАЗОМ, И ЭТО ВОПРОС ОХВАТА, А НЕ СКОРОСТИ. Поодиночке
    # девятьсот рынков занимали семь минут, поэтому мерились первые сто
    # восемьдесят — а какие в них попадут, решал порядок выдачи биржи.
    # Ликвидные, но непопулярные рынки лежат в хвосте любой выдачи: мы их не
    # отвергали, мы до них не доходили. Теперь доходим до всех.
    subset = rows[:int(limit)] if limit else rows
    tapes = book_mod.tape_many([r['condition_id'] for r in subset])
    for row in subset:
        live = books.get(str(row['token_id']))
        trades = tapes.get(row['condition_id']) or []
        mine = [t for t in as_yes(trades, row['token_id'], row.get('token_no'))
                if now - t['ts'] < day]
        span = max((max(t['ts'] for t in mine)
                    - min(t['ts'] for t in mine)) / 3600, 0.5) if mine else 1.0
        top = row.get('top') or (book_mod.top(live) if live else None)
        if not top or not live:
            row['wait_hours'] = float('inf')
            continue
        sells = [t for t in mine
                 if t['side'] == 'SELL' and t['price'] <= top['bid'] + 1e-9]
        buys = [t for t in mine
                if t['side'] == 'BUY' and t['price'] >= top['ask'] - 1e-9]
        flow_in = sum(t['size'] for t in sells) / span
        flow_out = sum(t['size'] for t in buys) / span
        size = row.get('size') or params.MM_MIN_ORDER_SIZE
        ticks = int(round(top['spread'] / row['tick'])) if row['tick'] > 0 else 0

        # ГЛУБИНА ШАГА ПОДБИРАЕТСЯ, А НЕ БЕРЁТСЯ В ОДИН ТИК.
        #
        # Шаг в один тик был произволом, и замер показал, насколько дорогим.
        # На широком спреде заявка в тик от лучшей цены стоит ДАЛЕКО от
        # середины, и до неё почти никто не доходит:
        #
        #     «Khvicha Kvaratskhelia», спред 21 тик
        #         шаг 1 тик:  остаётся 0.0190, поток   13.1 контр/ч
        #         шаг 5 тик:  остаётся 0.0110, поток  225.6 контр/ч
        #
        # Поток вырос в семнадцать раз, спред упал вдвое — произведение
        # выросло вдесятеро. По четырём рынкам подбор дал вдевятеро больше
        # жёсткого тика.
        #
        # Берём шаг с наибольшим произведением «что остаётся» на «что
        # доходит»: это и есть доход в единицу времени с точностью до размера.
        best = None
        for depth in _step_options(ticks):
            if depth == 0:
                price_bid, price_ask = top['bid'], top['ask']
                q_in = depth_at(live, 'bid', top['bid'])
                q_out = depth_at(live, 'ask', top['ask'])
            else:
                price_bid = top['bid'] + depth * row['tick']
                price_ask = top['ask'] - depth * row['tick']
                q_in = q_out = 0.0
            take = price_ask - price_bid
            if take <= 0:
                continue
            f_in = sum(t['size'] for t in mine if t['side'] == 'SELL'
                       and t['price'] <= price_bid + 1e-9) / span
            f_out = sum(t['size'] for t in mine if t['side'] == 'BUY'
                        and t['price'] >= price_ask - 1e-9) / span
            score = take * min(f_in, f_out)
            if best is None or score > best['score']:
                best = {'score': score, 'depth': depth, 'gain': take,
                        'flow_in': f_in, 'flow_out': f_out,
                        'queue_in': q_in, 'queue_out': q_out}
        if best is None:
            row['wait_hours'] = float('inf')
            continue

        row['step_ticks'] = best['depth']
        row['step_inside'] = best['depth'] > 0
        row['flow_in'] = round(best['flow_in'], 1)
        row['flow_out'] = round(best['flow_out'], 1)
        # Очереди сохраняются, потому что время придётся пересчитать: размер
        # заявки выбирается ПОЗЖЕ, а ждать большую заявку дольше.
        row['queue_in'] = round(best['queue_in'], 1)
        row['queue_out'] = round(best['queue_out'], 1)
        row['our_gain'] = round(max(best['gain'], 0.0), 6)
        _recompute_wait(row)
    for row in rows:
        row.setdefault('wait_hours', float('inf'))
        row.setdefault('usd_per_hour', 0.0)
        row.setdefault('our_gain', 0.0)
        row.setdefault('step_inside', False)
    return rows


def measure_activity(rows, limit=None):
    """
    Частота сделок по ЛЕНТЕ за последние сутки, а не по накопленному обороту.

    Оборот считается за всю жизнь рынка и потому льстит старым: $152 000 за
    год — это 0.3 сделки в час. Лента отвечает на нужный вопрос прямо: сколько
    раз в час здесь вообще торгуют, то есть сколько кругов мы можем надеяться
    совершить.

    Запрос идёт на каждый рынок отдельно, поэтому список сначала обрезается.
    Делать это каждый цикл было бы расточительством; частота меняется медленно,
    и пересчёта при перевыборе рынков достаточно.
    """
    day = 24 * 3600
    now = time.time()
    for row in rows[:int(limit)] if limit else rows:
        trades = book_mod.tape(row['condition_id'], limit=500) or []
        fresh = [t for t in trades if now - t['ts'] < day]
        row['trades_per_hour'] = round(len(fresh) / 24.0, 2)
        # Ожидаемый доход в час: что остаётся от спреда после шага внутрь,
        # умноженное на размер и на частоту. Это ПОТОЛОК — он считает, что
        # каждая чужая сделка могла бы стать нашим кругом.
        keep = max(row['spread'] - 2 * row['tick'], 0.0)
        row['usd_per_hour'] = round(keep * row['size'] * row['trades_per_hour'], 5)
    for row in rows:
        row.setdefault('trades_per_hour', 0.0)
        row.setdefault('usd_per_hour', 0.0)
    return rows


def _cap_per_event(rows):
    """
    Не больше нескольких рынков одного события.

    «США ударят по 8 странам» и «США ударят по 9 странам» — один и тот же
    вопрос, заданный дважды: запас по ним набирается ОДИНАКОВЫЙ и одинаково
    же обесценивается. Двадцать рынков одного события — это одна ставка с
    двадцатикратным размером, притом выглядящая как разнообразие.
    """
    seen, out = {}, []
    for row in rows:
        key = row.get('event_id') or row['condition_id']
        if seen.get(key, 0) >= params.MM_MAX_PER_EVENT:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(row)
    return out


def allocate(markets, budget=None):
    """
    Раскладывает бюджет по рынкам. Чем их больше, тем ровнее идёт результат.

    ПРЕДЕЛ НА ОДИН РЫНОК обязателен при малом счёте: без него сотня долларов
    уходила целиком в один рынок, и одно исполнение сажало половину счёта в
    позицию, которую нечем нести. При размере в пять контрактов ($5 на рынок)
    сотня расходится на двадцать рынков сама собой, и предел почти не мешает —
    но он остаётся на случай рынков с крупным минимальным размером.
    """
    budget = float(budget if budget is not None else params.bankroll_for('MM'))
    cap = budget * params.MM_MAX_MARKET_SHARE
    chosen, used, edge = [], 0.0, 0.0
    for market in markets:
        if market['cost'] > cap or used + market['cost'] > budget:
            continue
        chosen.append(market)
        used += market['cost']
        edge += market['spread_share'] * market['cost'] / 2
    rewards = sum(m['rewards_daily'] * m['cost'] / max(m['liquidity'] + m['cost'], 1)
                  for m in chosen)
    return {
        'markets': chosen, 'used': round(used, 2),
        'free': round(budget - used, 2), 'cap_per_market': round(cap, 2),
        # ПОТОЛОК дохода за один оборот всех рынков, а не ожидание. Считается
        # как половина спреда на вложенное — то есть при условии, что обе
        # стороны исполнились по нашим ценам и ни разу не против нас. Ни того,
        # ни другого мы пока не наблюдали: бумажный прогон не дал исполнений.
        'ceiling_per_round_usd': round(edge, 3),
        'rewards_daily': round(rewards, 3),
        'rewards_monthly': round(rewards * 30, 2),
    }
