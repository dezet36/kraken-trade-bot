"""
Отбор рынков под РАЗМЕР КАПИТАЛА. Для сотни долларов и для тысяч он разный.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ. При большом счёте доход даёт захват спреда: чем
больше исполнений, тем лучше. При сотне долларов всё наоборот — исполнения
приносят запас, который нечем нести: одна позиция в $20 это пятая часть счёта,
и один неудачный вход съедает месячную награду. Значит и рынки нужны другие, и
критерий другой.

ЧТО ИЗМЕРЕНО И ЧТО ИЗ ЭТОГО СЛЕДУЕТ

Награды платятся на 788 рынках, всего $5 976 в день. Соблазн — идти туда, где
награда велика относительно уже стоящей ликвидности: доля в пуле будет
наибольшей. Проверка показала, что так выбираются ПУСТЫЕ рынки:

    Conrad Kramer leave OpenAI    бидов на $2,  асков на $15,  спред 0.889
    Trump pardon Daniel Penny     бидов на $48, асков на $89,  спред 0.100

Награда там не разобрана не потому, что её не заметили. Чтобы её получить, надо
стоять в пределах 3.5-5.5 цента от середины, а в рынке со спредом 0.889 это
значит выставить узкую котировку туда, где никто не торгует. Первый же, кому
что-то понадобится, снимет нас по своей цене — и на сотне долларов это конец.

Отсюда главное условие отбора: СПРЕД РЫНКА УЖЕ ПОРОГА НАГРАДЫ. Тогда встать у
лучшей цены безопасно, и эта же цена сама попадает в зачёт. Таких рынков 667 из
796 — то есть отсев отбрасывает именно ловушку, а не возможности.

ЧЕСТНАЯ ОЦЕНКА ДОХОДА. При $100 в рынках с непустым стаканом выходит около
$0.39 в день. Это $12 в месяц. Годовых получается красиво, абсолютные числа
маленькие, и путать одно с другим не надо: доля в пуле оценивается по
ликвидности стакана, а зачитывается по очкам, которых мы не видим. Оценка
может ошибаться в обе стороны, и проверить её можно только живыми деньгами.
"""

import json
import time

from . import client, params


def _rewards_daily(market):
    return sum(float(r.get('rewardsDailyRate') or 0)
               for r in (market.get('clobRewards') or []))


def quote_cost(min_size, price):
    """
    Во что обходится минимальная ДВУСТОРОННЯЯ котировка.

    Обе стороны требуют капитала: покупка стоит p за контракт, продажа — (1-p),
    потому что продавать надо то, чего у нас нет, и биржа держит залог. Считать
    только одну сторону значило бы вдвое занизить потребность и обнаружить это
    на первом же отказе.
    """
    return float(min_size) * float(price) + float(min_size) * (1 - float(price))


def scan(budget=None, limit=None, pages=25):
    """
    Рынки, пригодные для нашего капитала, от лучших к худшим.

    Условия, и каждое из измеренного:

        награда платится                иначе стоять незачем
        спред УЖЕ порога награды        иначе qualifying-котировка означает
                                        узкую цену в мёртвом рынке
        минимальная котировка по карману  иначе не попадём в зачёт вовсе
        стакан не пустой                 пустой стакан — не возможность, а
                                        предупреждение
    """
    budget = float(budget if budget is not None else params.bankroll_for('MM'))
    limit = limit or params.MM_MARKETS
    rows = []
    for page in range(pages):
        chunk = client._get(f'{params.GAMMA}/markets?limit=100'
                            f'&offset={page * 100}&closed=false')
        if not isinstance(chunk, list) or not chunk:
            break
        for m in chunk:
            daily = _rewards_daily(m)
            if daily <= 0:
                continue
            max_spread = float(m.get('rewardsMaxSpread') or 0)
            spread = float(m.get('spread') or 0)
            if max_spread <= 0 or not 0 < spread <= max_spread / 100:
                continue
            liquidity = float(m.get('liquidity') or 0)
            if liquidity < params.MM_MIN_LIQUIDITY:
                continue
            try:
                tokens = json.loads(m.get('clobTokenIds') or '[]')
                price = float(json.loads(m.get('outcomePrices') or '[]')[0])
            except Exception:                              # noqa: BLE001
                continue
            if not tokens or not 0 < price < 1:
                continue
            min_size = float(m.get('rewardsMinSize') or 0)
            cost = quote_cost(min_size, price)
            if cost <= 0 or cost > budget:
                continue
            rows.append({
                'id': m.get('id'), 'question': m.get('question'),
                'condition_id': m.get('conditionId'), 'token_id': tokens[0],
                'tick': float(m.get('orderPriceMinTickSize') or 0.001),
                'rewards_daily': daily, 'rewardsMinSize': min_size,
                'rewardsMaxSpread': max_spread, 'liquidity': liquidity,
                'spread': spread, 'price': price, 'cost': cost,
                'end': m.get('endDate'), 'fee_type': m.get('feeType'),
                # Ожидаемая доля пула, если встанем минимальным размером.
                # Оценка ГРУБАЯ: доля считается по деньгам в стакане, а зачёт
                # идёт по очкам, которых мы не видим. Годится для порядка
                # величин и для сравнения рынков между собой, не более.
                'expected_daily': daily * cost / max(liquidity + cost, 1),
            })
        time.sleep(params.PAUSE)
    rows.sort(key=lambda r: -r['expected_daily'])
    return rows[:limit]


def allocate(markets, budget=None):
    """
    Раскладывает бюджет по рынкам, пока он не кончится.

    Берутся самые доходные на вложенный доллар. Отдельного «резерва» не
    оставляется намеренно: запас возникает от исполнений, а не от котировок, и
    держать деньги простаивающими на случай запаса значит платить за него
    дважды.
    """
    budget = float(budget if budget is not None else params.bankroll_for('MM'))
    # ПРЕДЕЛ НА ОДИН РЫНОК — не осторожность вообще, а следствие арифметики
    # малого счёта. Без него сотня долларов уходила целиком в один рынок:
    # самый доходный по оценке, но и единственный. Одно исполнение — и весь
    # счёт в одной позиции, которую нечем нести и нечем усреднить. Доход при
    # этом падает не сильно, а риск — во столько раз, во сколько выросло число
    # рынков.
    cap = budget * params.MM_MAX_MARKET_SHARE
    chosen, used, expected = [], 0.0, 0.0
    for market in markets:
        if market['cost'] > cap:
            continue
        if used + market['cost'] > budget:
            continue
        chosen.append(market)
        used += market['cost']
        expected += market['expected_daily']
    return {'markets': chosen, 'used': round(used, 2),
            'free': round(budget - used, 2),
            'expected_daily': round(expected, 4),
            'expected_monthly': round(expected * 30, 2),
            'cap_per_market': round(cap, 2)}
