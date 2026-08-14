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

        size = max(row['order_min'], params.MM_MIN_ORDER_SIZE)
        cost = quote_cost(size, top['bid'] + row['tick'])
        if cost <= 0 or cost > budget:
            continue
        row.update({
            'price': top['mid'], 'spread': top['spread'], 'top': top,
            'bid_usd': round(bid_usd, 2), 'size': size, 'cost': round(cost, 3),
            'ticks': ticks,
        })
        good.append(row)

    good = selector.measure_activity(good, limit=limit * 2)
    # Порядок по частоте: доход здесь — тик, и решает только то, сколько раз мы
    # успеем его взять. Ширина спреда не помогает, потому что выходим мы на
    # ОДИН тик выше входа, а не на половину спреда.
    good.sort(key=lambda r: -r['trades_per_hour'])
    return selector._cap_per_event(good)[:limit]


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
