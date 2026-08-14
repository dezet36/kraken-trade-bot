"""
Односторонний мейкер на дешёвых рынках: покупаем внизу, выходим на тик выше.

ЗАЧЕМ ОТДЕЛЬНАЯ СТРАТЕГИЯ, А НЕ НАСТРОЙКА ДВУСТОРОННЕЙ. Двусторонняя не
набирает направленной позиции: наклон котировки против запаса возвращает её к
нулю. Эта — набирает всегда и только в одну сторону. Судьбу её решает не спред,
а то, что происходит с накопленным, и меряется она поэтому иначе. Смешать их в
одном модуле значило бы смешать и проверки.

ОТКУДА БЕРЁТСЯ ВЫГОДА ПО КАПИТАЛУ. Двусторонняя котировка стоит РОВНО размер
при любой цене: покупка берёт p, продажа (1-p). Односторонняя берёт только p.
На цене 0.10 это $0.50 вместо $5 — вдесятеро больше рынков на те же деньги.
Выход по капиталу бесплатен: продаём то, чем уже владеем.

ЧТО ИЗМЕРЕНО ПЕРЕД ТЕМ, КАК ПИСАТЬ. Критерии записаны заранее в
Research/ONESIDE_CRITERIA.md. На 3 001 разрешённом рынке и 375 независимых
событиях недобор (реальная частота «да» минус уплаченная цена) в полосе
0.02-0.15 составил +0.0191 на контракт, интервал [-0.0034, +0.0446].

Отбраковка требовала верхней границы ниже -0.01 — не сработала. Полного
одобрения (нижняя граница не ниже нуля) тоже нет: -0.0034. Значит накопление
безобидно, а стратегия стоит или падает на частоте выходов. При худшем конце
интервала достаточно, чтобы выход находился у каждой четвёртой покупки:
0.01·f - 0.0034·(1-f) > 0 при f > 25%.

ПЕРВЫЙ ОТВЕТ БЫЛ ВТРОЕ КРАСИВЕЕ И ОКАЗАЛСЯ АРТЕФАКТОМ. Взвешивание по
наблюдениям цены давало недобор +0.1062: рынки, подолгу стоявшие в полосе,
считались сотни раз каждый. Единица счёта — рынок, а не наблюдение.

ЧЕМ МЫ ОТЛИЧАЕМСЯ ОТ РАЗОБРАННОГО КОШЕЛЬКА. У @planktonxd 2 236 накопленных
позиций и переоценка -$8 564 при +$11 000 зафиксированных. Разница одна: он
докупает, мы — нет. Потолок в одну партию на рынок держит накопление
ограниченным числом рынков, а не временем работы.
"""

import time

from . import book as book_mod
from . import client, params, selector


def is_cheap(price):
    """Цена в полосе, где замерялся недобор. Вне полосы замера нет."""
    return params.OS_MIN_PRICE <= float(price) <= params.OS_MAX_PRICE


def quote_cost(size, price):
    """
    Во что обходится ОДНОСТОРОННЯЯ котировка — только покупка.

    Здесь и живёт вся выгода по капиталу: двусторонняя стоила бы размер целиком
    при любой цене, эта — только p за контракт. На 0.10 разница десятикратная.
    """
    return float(size) * float(price)


def scan(budget=None, limit=None, pages=30):
    """
    Дешёвые рынки с живым бидом, где есть куда встать.

    ГЛУБИНА ТРЕБУЕТСЯ ТОЛЬКО ПО БИДУ, и это не послабление, а следствие: мы не
    продаём того, чего нет, значит сторона аска нас не касается до первой
    покупки. Но пустой бид по-прежнему отсекается — там наша заявка окажется
    единственной, и исполнят нас ровно тогда, когда это выгодно встречной
    стороне.
    """
    budget = float(budget if budget is not None else params.bankroll_for('MM'))
    limit = int(limit or params.OS_MARKETS)
    rows = selector._candidates(pages, params.OS_MIN_VOLUME,
                                params.OS_MIN_PRICE, params.OS_MAX_PRICE)
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
        if not is_cheap(top['mid']):
            continue
        bid_usd = sum(size * price for price, size in live['bids'])
        if bid_usd < params.OS_MIN_BID_DEPTH:
            continue
        # Нужно место, чтобы встать внутрь и потом выйти выше. Два тика — это
        # минимум: один на вход, один на выход.
        ticks = int(round(top['spread'] / row['tick'])) if row['tick'] > 0 else 0
        if ticks < params.OS_MIN_TICKS:
            continue

        row.update({
            'price': top['mid'], 'spread': top['spread'], 'top': top,
            'bid_usd': round(bid_usd, 2), 'ticks': ticks,
            'size': max(row['order_min'], params.MM_MIN_ORDER_SIZE),
        })
        good.append(row)

    good = measure_two_way(good, limit=limit * 2)
    # ПОТОК ДОЛЖЕН ИДТИ В ОБЕ СТОРОНЫ. Это условие добавлено после замера, и
    # без него стратегия была бы убыточной.
    #
    # Замер по ленте: на дешёвых рынках медианная доля выходов равна НУЛЮ. Есть
    # продавцы по нашей цене входа и НИ ОДНОГО покупателя по цене выхода —
    # поток односторонний. «Milei out as President», «Will Arc launch a token»:
    # по две продажи в час и ни одной покупки выше. Купив там, мы не вышли бы
    # никогда и получили бы ровно те 2 236 висящих позиций разобранного
    # кошелька.
    #
    # Дешёвый лонгшот тем и дёшев, что все хотят из него выйти. Мейкер нужен
    # рынку именно там, но платят за это не спредом, а тем, что мы остаёмся с
    # бумагой на руках.
    good = keep_two_way(good)
    # Порядок по числу ПОЛНЫХ кругов: их не больше, чем меньшая из сторон.
    # Считать по входам значило бы хвалить рынок за то, что нас там охотно
    # нагружают.
    good.sort(key=lambda r: -min(r['in_per_hour'], r['out_per_hour']))
    good = selector._cap_per_event(good)[:limit]
    for row in good:
        row['size'] = size_for(row, budget)
        row['cost'] = round(quote_cost(row['size'],
                                       row['top']['bid'] + row['tick']), 3)
    return [r for r in good if r['cost'] > 0]


def keep_two_way(rows):
    """Оставляет рынки, куда не только продают нам, но и покупают обратно."""
    return [r for r in rows
            if float(r.get('in_per_hour') or 0) > 0
            and float(r.get('exit_share') or 0) >= params.OS_MIN_EXIT_SHARE]


def size_for(row, budget):
    """
    Размер заявки по ПОТОКУ рынка, а не пятёрка на всё подряд.

    ПОЧЕМУ ПЯТЁРКА БЫЛА ОШИБКОЙ. После порога на двусторонний поток остаётся
    горстка рынков, и на них уходило $2.39 из ста долларов: 97% счёта стояло
    без дела. При этом замеренный поток продаж по нашим ценам составляет 299
    контрактов в час, а медианная чужая сделка — от 7 до 357 контрактов. Наши
    пять — это 12% часового потока: мы просили меньше, чем рынок готов дать.

    ТРИ ПОТОЛКА, И КАЖДЫЙ ОТ СВОЕЙ БЕДЫ:

        часы потока   больше, чем рынок пропускает за это время, взять нельзя —
                      остаток заявки просто простоит;
        доля счёта    один рынок не должен забирать заметную часть денег, иначе
                      его разрешение станет событием для всего счёта;
        доля стакана  быть большей частью бида значит двигать цену собой и
                      остаться единственным покупателем, когда придёт продавец,
                      который знает больше.
    """
    flow = float(row.get('in_size_per_hour') or 0) * params.OS_FLOW_HOURS
    entry = row['top']['bid'] + row['tick']
    by_money = (budget * params.OS_MAX_MARKET_SHARE) / max(entry, 1e-9)
    by_book = (row['bid_usd'] * params.OS_MAX_BOOK_SHARE) / max(entry, 1e-9)
    size = min(flow, by_money, by_book)
    floor = max(row['order_min'], params.MM_MIN_ORDER_SIZE)
    if size < floor:
        return floor
    return float(int(size))


def measure_two_way(rows, limit=None):
    """
    Сколько раз в сутки нас могли бы КУПИТЬ и сколько раз — продать нам обратно.

    Считается по ленте на НАШИХ ценах: вход — продажи не выше нашего бида,
    выход — покупки не ниже нашего аска. Общая частота сделок для этого не
    годится: она складывает обе стороны и потому не отличает рынок, где идёт
    обмен, от рынка, откуда все бегут.
    """
    day = 24 * 3600
    now = time.time()
    for row in rows[:int(limit)] if limit else rows:
        trades = book_mod.tape(row['condition_id'], limit=500) or []
        mine = [t for t in trades
                if t.get('asset') == row['token_id'] and now - t['ts'] < day]
        top, tick = row['top'], row['tick']
        entry = round(top['bid'] + tick, 10)
        exit_price = round(entry + tick, 10)
        span = max((max(t['ts'] for t in mine)
                    - min(t['ts'] for t in mine)) / 3600, 0.5) if mine else 1.0
        ins = [t for t in mine
               if t['side'] == 'SELL' and t['price'] <= entry + 1e-9]
        outs = [t for t in mine
                if t['side'] == 'BUY' and t['price'] >= exit_price - 1e-9]
        row['in_per_hour'] = round(len(ins) / span, 3)
        row['out_per_hour'] = round(len(outs) / span, 3)
        # ПОТОК В КОНТРАКТАХ, А НЕ В СДЕЛКАХ. Размер заявки считается отсюда, и
        # спутать одно с другим значит занизить его в десятки раз: чужая сделка
        # бывает и в 357 контрактов, а «сделок в час» бывает 1.3.
        row['in_size_per_hour'] = round(sum(t['size'] for t in ins) / span, 2)
        row['out_size_per_hour'] = round(sum(t['size'] for t in outs) / span, 2)
        row['exit_share'] = round(
            min(1.0, row['out_per_hour'] / row['in_per_hour']), 3) \
            if row['in_per_hour'] > 0 else 0.0
        row['trades_per_hour'] = round(len(mine) / span, 2)
    for row in rows:
        row.setdefault('in_per_hour', 0.0)
        row.setdefault('out_per_hour', 0.0)
        row.setdefault('in_size_per_hour', 0.0)
        row.setdefault('out_size_per_hour', 0.0)
        row.setdefault('exit_share', 0.0)
        row.setdefault('trades_per_hour', 0.0)
    return rows


def desired_quote(top, market, position=0.0):
    """
    Куда встать. Без позиции — только бид; с позицией — только аск.

    СТОРОНЫ НИКОГДА НЕ КОТИРУЮТСЯ ВМЕСТЕ, и это главное ограничение риска.
    Докупка к имеющейся позиции — ровно тот путь, которым разобранный кошелёк
    набрал 2 236 позиций и переоценку -$8 564: каждая покупка по отдельности
    выглядит дешёвой, а накопление не имеет предела во времени.

    С потолком в одну партию накопление ограничено ЧИСЛОМ рынков, а не сроком
    работы: сколько рынков, столько партий, и ни контрактом больше.
    """
    if not top or top.get('bid') is None or top.get('ask') is None:
        return None
    tick = float(market.get('tick') or 0.001)
    size = max(float(market.get('order_min') or 0), params.MM_MIN_ORDER_SIZE)
    ticks = int(round((top['ask'] - top['bid']) / tick)) if tick > 0 else 0

    if position <= 0:
        if not is_cheap(top['mid']):
            return {'reason': 'цена вне полосы, где мерился недобор'}
        if ticks < params.OS_MIN_TICKS:
            return {'reason': f'спред {ticks} тик(а) — встали бы в конец очереди'}
        price = round(top['bid'] + tick, 10)
        if price >= top['ask']:
            return {'reason': 'шаг внутрь пересёк бы рынок'}
        return {'side': 'bid', 'price': price, 'size': size,
                'cost': quote_cost(size, price), 'reason': ''}

    # ВЫХОД КОТИРУЕТСЯ ВСЕГДА, даже когда он невыгоден. Позиция без выставленного
    # аска — это ставка на исход, а не работа мейкера: мы перестаём предлагать
    # ликвидность и начинаем ждать разрешения.
    price = round(max(top['bid'] + tick, market.get('avg_cost', 0) + tick), 10)
    price = min(price, round(top['ask'] - tick, 10)) if ticks >= 2 else price
    if price <= 0 or price >= 1:
        return {'reason': 'цена выхода вне допустимого диапазона'}
    return {'side': 'ask', 'price': price, 'size': min(size, abs(position)),
            'cost': 0.0, 'reason': ''}


def plan(markets, budget=None):
    """
    Раскладка бюджета. Рынков помещается вдесятеро больше двусторонней схемы.

    Потолок на рынок здесь не нужен: одна партия по цене ниже 0.15 не может
    занять существенную долю счёта. Нужен потолок на СУММУ — накопление
    ограничивается числом рынков, и число это выбирается здесь.
    """
    budget = float(budget if budget is not None else params.bankroll_for('MM'))
    chosen, used = [], 0.0
    for market in markets:
        if used + market['cost'] > budget:
            continue
        chosen.append(market)
        used += market['cost']
    worst = sum(m['cost'] for m in chosen)
    return {
        'markets': chosen, 'used': round(used, 2),
        'free': round(budget - used, 2),
        # ХУДШИЙ СЛУЧАЙ НАЗЫВАЕТСЯ ПРЯМО: если КАЖДЫЙ рынок исполнится и
        # КАЖДЫЙ разрешится против нас, мы теряем всё вложенное. Замер говорит,
        # что разрешаются они примерно по цене, но замер — не гарантия.
        'worst_case_usd': round(worst, 2),
        'ceiling_per_round_usd': round(
            sum(m['size'] * m['tick'] for m in chosen), 3),
        'trades_per_hour': round(sum(m.get('trades_per_hour', 0)
                                     for m in chosen), 2),
    }
