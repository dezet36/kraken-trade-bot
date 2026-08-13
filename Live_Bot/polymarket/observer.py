"""
Фаза 0: наблюдение за стаканом. Собирает то, чего нет ни в одной истории цен.

ЗАЧЕМ ЭТО ВООБЩЕ НУЖНО. Маркет-мейкинг нельзя проверить на исторических ценах.
Он живёт двумя величинами, которых там не существует: исполнилась бы наша
заявка (история хранит сделки, а не очередь) и кто кого снял (нас исполняют
тогда, когда встречной стороне это выгодно). Единственный способ их получить —
котировать понарошку поверх ЖИВОГО стакана и записывать, что было дальше.

ЧТО ЗАПИСЫВАЕТСЯ КАЖДЫЙ ЦИКЛ

    состояние стакана        лучшие цены, спред, середина, глубина
    наша воображаемая заявка обе стороны, размер, очередь перед нами
    право на награду         проходит ли котировка по правилам биржи
    судьба прошлых заявок    исполнились ли, когда, и куда ушла цена ПОСЛЕ

Последняя строка — самая ценная. Разница между ценой нашего исполнения и
серединой рынка через пять минут и есть неблагоприятный отбор, то есть плата за
то, что нас снял тот, кто знал больше. Если она съедает захваченный спред,
стратегия не работает, и это выяснится ДО первого доллара.

НИЧЕГО НЕ ОТПРАВЛЯЕТСЯ НА БИРЖУ. Модуль только читает. Ни ключей, ни подписей,
ни заявок — по построению, а не по настройке.

Запуск:
    python -m polymarket.observer            один цикл
    python -m polymarket.observer --loop     непрерывно
"""

import json
import os
import sys
import time

import config

from . import book, client, params, store

OBSERVATIONS = os.path.join(store.DIR, 'book_observations.jsonl')
QUOTES = os.path.join(store.DIR, 'quotes.jsonl')


def _stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def pick_markets(limit=None, min_liquidity=None):
    """
    Рынки для наблюдения: с наградой, с живым двусторонним стаканом.

    Отбор идёт по НАГРАДЕ, а не по обороту, и это исправление уже сделанной
    ошибки. Первый замер смотрел сотню крупнейших по обороту рынков и нашёл
    награды лишь на пятнадцати, заключив, что программа ничтожна ($88 в день).
    По всем 2100 активным рынкам награды платятся на 781 и составляют $5 976 в
    день: платят не там, где самый большой оборот.
    """
    limit = limit or params.MM_MARKETS
    min_liquidity = min_liquidity or params.MM_MIN_LIQUIDITY
    rows = []
    for page in range(20):
        chunk = client._get(f'{params.GAMMA}/markets?limit=100'
                            f'&offset={page * 100}&closed=false')
        if not isinstance(chunk, list) or not chunk:
            break
        for m in chunk:
            daily = sum(float(r.get('rewardsDailyRate') or 0)
                        for r in (m.get('clobRewards') or []))
            if daily <= 0:
                continue
            if float(m.get('liquidity') or 0) < min_liquidity:
                continue
            try:
                tokens = json.loads(m.get('clobTokenIds') or '[]')
            except Exception:                              # noqa: BLE001
                continue
            if not tokens:
                continue
            rows.append({
                'id': m.get('id'), 'question': m.get('question'),
                'condition_id': m.get('conditionId'),
                'token_id': tokens[0],
                'tick': float(m.get('orderPriceMinTickSize') or 0.001),
                'rewards_daily': daily,
                'rewardsMinSize': m.get('rewardsMinSize'),
                'rewardsMaxSpread': m.get('rewardsMaxSpread'),
                'liquidity': float(m.get('liquidity') or 0),
                'end': m.get('endDate'),
            })
        time.sleep(params.PAUSE)
    # Сначала те, где награда крупнее относительно уже стоящей ликвидности:
    # именно там наша доля в пуле будет заметной. Делить на ликвидность, а не
    # брать награду как есть, обязательно — иначе выберутся рынки, где пул
    # большой, но и желающих делить его столько же.
    rows.sort(key=lambda r: r['rewards_daily'] / max(r['liquidity'], 1),
              reverse=True)
    return rows[:limit]


def observe(market):
    """Один снимок рынка вместе с воображаемой котировкой."""
    live = book.fetch(market['token_id'])
    if live is None:
        return None
    info = book.top(live)
    if not info or info['bid'] is None or info['ask'] is None:
        return {'at': _stamp(), 'market': market['id'], 'skip': 'стакан односторонний'}

    # РАЗМЕР БЕРЁТСЯ ОТ РЫНКА, А НЕ ОБЩИЙ, И ЭТО НАШЛОСЬ ПЕРВЫМ ЖЕ ПРОГОНОМ.
    # Порог `rewardsMinSize` у рынков разный: 20, 30, 50, 100, 200. С общим
    # размером 100 семь рынков из двадцати пяти не проходили под награду — то
    # есть мы стояли бы в стакане, неся риск, и не получали за это ничего.
    need = float(market.get('rewardsMinSize') or 0)
    quoted = book.quote(live, market['tick'], step=params.MM_STEP_TICKS,
                        min_size=max(params.MM_QUOTE_SIZE, need))
    reward = book.rewards_eligible(quoted, market, live)
    row = {
        'at': _stamp(), 'ts': int(time.time()),
        'market': market['id'], 'token': market['token_id'],
        'condition': market['condition_id'],
        'question': market['question'],
        'tick': market['tick'],
        'best_bid': info['bid'], 'best_ask': info['ask'],
        'spread': info['spread'], 'mid': info['mid'],
        'bid_size': info['bid_size'], 'ask_size': info['ask_size'],
        'levels_bid': len(live['bids']), 'levels_ask': len(live['asks']),
        'rewards_daily': market['rewards_daily'],
        'liquidity': market['liquidity'],
    }
    if quoted:
        row.update({
            'our_bid': quoted['bid'], 'our_ask': quoted['ask'],
            'our_size': quoted['size'],
            'queue_bid': book.depth_ahead(live, 'bid', quoted['bid']),
            'queue_ask': book.depth_ahead(live, 'ask', quoted['ask']),
            'reward_ok': reward['eligible'], 'reward_why': reward['why'],
        })
    return row


def resolve_quotes(market, pending, now_mid):
    """
    Что случилось с ранее выставленными воображаемыми заявками.

    Возвращает список закрытых записей. Заявка считается исполненной, если по
    ленте через её цену прошло больше объёма, чем стояло перед ней; модель
    очереди намеренно пессимистична и описана в book.would_fill.
    """
    if not pending:
        return []
    trades = book.tape(market['condition_id'])
    if trades is None:
        return []
    done = []
    for item in pending:
        fresh = [t for t in trades if t['ts'] >= item['ts']]
        for side, price, queue in (('bid', item.get('our_bid'), item.get('queue_bid')),
                                   ('ask', item.get('our_ask'), item.get('queue_ask'))):
            if price is None:
                continue
            verdict = book.would_fill(side, price, queue, fresh,
                                      token_id=market['token_id'])
            if not verdict or not verdict['filled']:
                continue
            # ПЛАТА ЗА НЕБЛАГОПРИЯТНЫЙ ОТБОР считается в сторону НАШЕЙ позиции:
            # купили — плохо, если середина упала; продали — если выросла.
            drift = None
            if now_mid is not None:
                drift = (now_mid - price) if side == 'bid' else (price - now_mid)
            done.append({
                'at': _stamp(), 'market': market['id'],
                'question': market['question'],
                'side': side, 'price': price,
                'queue_ahead': queue,
                'filled_ts': verdict['ts'],
                'seconds_to_fill': verdict['ts'] - item['ts'],
                'mid_at_quote': item.get('mid'),
                'mid_now': now_mid,
                'drift': drift,
                'spread_at_quote': item.get('spread'),
                'reward_ok': item.get('reward_ok'),
            })
    return done


def cycle(markets=None, pending=None):
    """Один проход: снять стаканы, записать, разобрать судьбу прошлых заявок."""
    markets = markets if markets is not None else pick_markets()
    pending = pending if pending is not None else {}
    fresh, closed = 0, 0
    for market in markets:
        row = observe(market)
        if row is None:
            continue
        store._append(OBSERVATIONS, row)
        fresh += 1
        # Заявки старше пяти минут разбираем: этого хватает, чтобы лента успела
        # показать исполнение, и мало, чтобы наблюдение оставалось свежим.
        key = str(market['id'])
        queue = pending.setdefault(key, [])
        ripe = [q for q in queue if row['ts'] - q['ts'] >= 300]
        if ripe:
            for done in resolve_quotes(market, ripe, row.get('mid')):
                store._append(QUOTES, done)
                closed += 1
            pending[key] = [q for q in queue if q not in ripe]
        if row.get('our_bid') is not None:
            pending[key].append(row)
        time.sleep(params.PAUSE)
    return {'observed': fresh, 'resolved': closed, 'pending': pending}


def main(loop=False):
    markets = pick_markets()
    print(f'наблюдаем рынков: {len(markets)}')
    for m in markets[:8]:
        print(f'   ${m["rewards_daily"]:>5.0f}/день  ликв ${m["liquidity"]:>9,.0f}  '
              f'{str(m["question"])[:52]}')
    pending = {}
    while True:
        result = cycle(markets, pending)
        pending = result['pending']
        print(f'[{_stamp()}] снимков {result["observed"]}, '
              f'разобрано заявок {result["resolved"]}')
        if not loop:
            return result
        time.sleep(params.MM_POLL_SECONDS)


if __name__ == '__main__':
    main(loop='--loop' in sys.argv)
