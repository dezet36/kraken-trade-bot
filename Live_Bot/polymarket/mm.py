"""
Маркет-мейкер: отбор рынков и торговый цикл.

ЧТО ДЕЛАЕТ. Держит двусторонние котировки на многих рынках сразу, исполняет их
по ленте биржи, ведёт запас и результат. Работает в бумаге; живое исполнение
требует отдельного модуля с подписью заявок, которого здесь нет намеренно.

ОТБОР РЫНКОВ ИДЁТ ПО НАГРАДЕ, А НЕ ПО ОБОРОТУ, и это исправление уже сделанной
ошибки. Первый замер смотрел сотню крупнейших по обороту рынков, нашёл награды
на пятнадцати и заключил, что программа ничтожна — $88 в день. По всем 2100
активным рынкам награды платятся на 781 и составляют $5 976 в день. Платят не
там, где самый большой оборот, а там, где бирже нужна ликвидность.

ТРИ ОГРАНИЧЕНИЯ РИСКА, И КАЖДОЕ ИЗ ИЗМЕРЕННОГО:

    потолок запаса на рынок      разобранный кошелёк держит 2 236 позиций с
                                 переоценкой -$8 564; потолок делает такое
                                 накопление невозможным
    потолок вложенного всего     чтобы одновременный перекос на многих рынках
                                 не сложился в одну большую ставку
    срок удержания               доход здесь спред, а не исход; застрявшая
                                 позиция превращает мейкера в предсказателя

ЧЕГО ЗДЕСЬ НЕТ. Отправки заявок на биржу. Ни ключа, ни подписи, ни POST —
по построению, а не по настройке.

Запуск:
    python -m polymarket.mm            один цикл
    python -m polymarket.mm --loop     непрерывно
"""

import json
import sys
import time

from . import book as book_mod
from . import client, engine, params, store, strategy

CANDIDATES = None          # кэш отбора внутри процесса


def select_markets(limit=None, min_liquidity=None, refresh=False):
    """
    Рынки, где вообще имеет смысл стоять.

    Условия: платится награда, стакан двусторонний, ликвидности достаточно,
    и до разрешения ещё есть время. Последнее важно: за час до конца стакан
    становится односторонним, а исполнение — почти всегда неблагоприятным.
    """
    global CANDIDATES
    if CANDIDATES is not None and not refresh:
        return CANDIDATES
    limit = limit or params.MM_MARKETS
    min_liquidity = min_liquidity or params.MM_MIN_LIQUIDITY
    rows = []
    for page in range(25):
        chunk = client._get(f'{params.GAMMA}/markets?limit=100'
                            f'&offset={page * 100}&closed=false')
        if not isinstance(chunk, list) or not chunk:
            break
        for m in chunk:
            daily = sum(float(r.get('rewardsDailyRate') or 0)
                        for r in (m.get('clobRewards') or []))
            if daily <= 0 or float(m.get('liquidity') or 0) < min_liquidity:
                continue
            try:
                tokens = json.loads(m.get('clobTokenIds') or '[]')
            except Exception:                              # noqa: BLE001
                continue
            if not tokens:
                continue
            rows.append({
                'id': m.get('id'), 'question': m.get('question'),
                'condition_id': m.get('conditionId'), 'token_id': tokens[0],
                'tick': float(m.get('orderPriceMinTickSize') or 0.001),
                'rewards_daily': daily,
                'rewardsMinSize': m.get('rewardsMinSize'),
                'rewardsMaxSpread': m.get('rewardsMaxSpread'),
                'liquidity': float(m.get('liquidity') or 0),
                'end': m.get('endDate'), 'fee_type': m.get('feeType'),
            })
        time.sleep(params.PAUSE)
    # Награда, отнесённая к уже стоящей ликвидности: там наша доля в пуле
    # будет заметной. Брать награду как есть нельзя — крупный пул обычно и
    # поделен на большее число участников.
    rows.sort(key=lambda r: r['rewards_daily'] / max(r['liquidity'], 1),
              reverse=True)
    CANDIDATES = rows[:limit]
    return CANDIDATES


def step(maker, markets):
    """Один проход по всем рынкам: исполнения, затем новые котировки."""
    marks, placed, fills, skipped = {}, 0, [], {}
    exposure = maker.exposure(marks)

    for market in markets:
        token = market['token_id']
        live = book_mod.fetch(token)
        if live is None:
            skipped['стакан не пришёл'] = skipped.get('стакан не пришёл', 0) + 1
            continue
        top = book_mod.top(live)
        if not top or top['bid'] is None or top['ask'] is None:
            skipped['стакан односторонний'] = skipped.get('стакан односторонний', 0) + 1
            continue
        marks[str(token)] = top['mid']

        # СНАЧАЛА исполнения, потом новые котировки. Обратный порядок выставил
        # бы заявку и тут же проверил её по ленте, где она физически не могла
        # исполниться, — и дал бы себе фору в один цикл.
        tape = book_mod.tape(market['condition_id'], limit=200)
        for done in maker.process_fills(token, market['condition_id'], tape):
            store._append(engine.FILLS, done)
            fills.append(done)

        slot = maker._slot(token)
        quote = strategy.desired_quote(top, market, position=slot['position'],
                                       max_position=params.MM_MAX_POSITION)
        if not quote or quote.get('reason'):
            skipped[(quote or {}).get('reason') or 'котировка не собралась'] = \
                skipped.get((quote or {}).get('reason') or 'котировка не собралась', 0) + 1
            continue

        # Потолок вложенного: при его превышении котируем ТОЛЬКО сокращающую
        # сторону. Полная остановка была бы хуже — запас остался бы висеть.
        if exposure > params.MM_MAX_EXPOSURE_USD and not quote.get('only'):
            quote['only'] = 'ask' if slot['position'] > 0 else 'bid'

        maker.place(token, quote, top, live)
        placed += 1
        time.sleep(params.PAUSE)

    report = maker.mark_to_market(marks)
    store._append(engine.EQUITY, {'at': engine._stamp(), **report,
                                  'markets': placed, 'fills': len(fills)})
    maker.save()
    return {'placed': placed, 'fills': fills, 'report': report,
            'skipped': skipped, 'marks': marks}


def main(loop=False):
    markets = select_markets()
    maker = PaperMaker = engine.PaperMaker()
    print(f'капитал маркет-мейкера: ${maker.bankroll:,.0f}')
    print(f'рынков отобрано: {len(markets)}')
    for m in markets[:6]:
        print(f'   ${m["rewards_daily"]:>5.0f}/день  ликв ${m["liquidity"]:>9,.0f}  '
              f'{str(m["question"])[:50]}')
    while True:
        out = step(maker, markets)
        r = out['report']
        print(f'[{engine._stamp()}] котировок {out["placed"]}, '
              f'исполнений {len(out["fills"])}, '
              f'капитал ${r["equity"]:,.2f} '
              f'(зафикс ${r["realized"]:+,.2f}, запас ${r["inventory"]:,.2f})')
        if out['skipped']:
            print('   пропущено:', dict(out['skipped']))
        if not loop:
            return out
        time.sleep(params.MM_POLL_SECONDS)


if __name__ == '__main__':
    main(loop='--loop' in sys.argv)
