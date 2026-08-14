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
                'tick': float(m.get('orderPriceMinTickSize') or 0.001),
                'order_min': float(m.get('orderMinSize') or 5),
                'rewards_daily': rewards_daily(m),
                'rewardsMinSize': m.get('rewardsMinSize'),
                'rewardsMaxSpread': m.get('rewardsMaxSpread'),
                'liquidity': float(m.get('liquidity') or 0),
                'volume': float(m.get('volume') or 0),
                'meta_price': price, 'meta_spread': spread,
                'end': m.get('endDate'), 'fee_type': m.get('feeType'),
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
    limit = limit or params.MM_MARKETS
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
        balance = min(bid_usd, ask_usd) / max(bid_usd, ask_usd, 1e-9)
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
        ticks = int(round(top['spread'] / row['tick'])) if row['tick'] > 0 else 0
        if ticks < params.MM_MIN_TICKS_TO_STEP_IN:
            continue

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

    # ПОРЯДОК ПО АКТИВНОСТИ, А НЕ ПО ШИРИНЕ СПРЕДА. Доход мейкера даёт частота
    # оборотов, а не ширина: у разобранного кошелька 1 495 сделок в сутки по
    # $1.79, и живёт он этим. Сортировка по спреду поднимала наверх рынки, где
    # спред 0.56 при цене 0.42 — то есть бид 0.14 против аска 0.70. Там никто
    # не котирует теснее не по щедрости, а потому что исход неясен.
    #
    # Слишком узкий спред тоже не годится: если он равен тику, круг не покроет
    # даже проскальзывания. Поэтому порог снизу, а дальше — оборот.
    good = [r for r in good
            if r['spread_share'] >= params.MM_MIN_SPREAD_SHARE
            and r['spread_share'] <= params.MM_MAX_SPREAD_SHARE]
    good.sort(key=lambda r: -r['volume'])
    return good[:limit]


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
