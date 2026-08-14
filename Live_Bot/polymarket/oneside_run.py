"""
Прогон одностороннего мейкера. Отдельный от двустороннего, включая состояние.

СОСТОЯНИЕ ОТДЕЛЬНОЕ НАМЕРЕННО. Две стратегии в одном файле учёта смешали бы
свои позиции и результаты, и ответить, какая из них зарабатывает, стало бы
нечем. Это тот же довод, по которому у них разные параметры.

ЧТО МЕРЯЕМ. Замер недобора уже сделан на истории: накопление в полосе 0.02-0.15
безобидно (+0.0191 на контракт, интервал [-0.0034, +0.0446]). Осталось
единственное неизвестное — ДОЛЯ ПОКУПОК, НАХОДЯЩИХ ВЫХОД. При худшем конце
интервала достаточно, чтобы выход находила каждая четвёртая:

    0.01·f - 0.0034·(1-f) > 0   при   f > 25%

Эту долю история не подскажет: она зависит от очереди, от того, кто ещё стоит в
стакане, и от скорости. Даёт её только прогон.

Запуск:
    python -m polymarket.oneside_run          один цикл
    python -m polymarket.oneside_run --loop   непрерывно
"""

import json
import os
import sys
import time

from . import book as book_mod
from . import engine, oneside, params, store

STATE = os.path.join(store.DIR, 'os_state.json')
FILLS = os.path.join(store.DIR, 'os_fills.jsonl')
EQUITY = os.path.join(store.DIR, 'os_equity.jsonl')

MARKETS = None
LAST_PLAN = {}


def select(budget=None, refresh=False):
    global MARKETS
    if MARKETS is not None and not refresh:
        return MARKETS
    money = float(budget if budget is not None else params.bankroll_for('ONESIDE'))
    rows = oneside.scan(budget=money, limit=params.OS_MARKETS)
    got = oneside.plan(rows, budget=money)
    MARKETS = got['markets']
    LAST_PLAN.clear()
    LAST_PLAN.update(got)
    return MARKETS


def step(maker, markets):
    """
    Один проход: стаканы пачкой, исполнения по ленте, новые заявки.

    ЛЕНТА БЕРЁТСЯ ТОЛЬКО ТАМ, ГДЕ У НАС ЕСТЬ ЖИВАЯ ЗАЯВКА. Рынков здесь за
    двести, и запрашивать ленту у каждого каждый цикл значило бы потратить на
    это весь цикл. Там, где заявки нет, исполниться нечему.
    """
    by_token = {m['token_id']: m for m in markets}
    books = book_mod.fetch_many(list(by_token))
    marks, placed, fills, skipped = {}, 0, [], {}
    entries, exits = 0, 0

    for token, market in by_token.items():
        live = books.get(str(token))
        if not live:
            skipped['стакан не пришёл'] = skipped.get('стакан не пришёл', 0) + 1
            continue
        top = book_mod.top(live)
        if not top or top['bid'] is None or top['ask'] is None:
            skipped['стакан односторонний'] = skipped.get('стакан односторонний', 0) + 1
            continue
        marks[str(token)] = top['mid']

    committed = 0.0
    budget = float(maker.state['cash'])

    for token, market in by_token.items():
        live = books.get(str(token))
        if not live or str(token) not in marks:
            continue
        top = book_mod.top(live)
        slot = maker._slot(token)

        if (slot.get('orders') or {}).get('bid') or (slot.get('orders') or {}).get('ask'):
            tape = book_mod.tape(market['condition_id'], limit=200)
            for done in maker.process_fills(token, market['condition_id'], tape):
                store._append(FILLS, done)
                fills.append(done)

        quote = oneside.desired_quote(
            top, dict(market, avg_cost=slot.get('avg_cost') or 0.0),
            position=slot['position'])
        if not quote or quote.get('reason'):
            reason = (quote or {}).get('reason') or 'котировка не собралась'
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        # ВХОД ТРЕБУЕТ ДЕНЕГ, ВЫХОД — НЕТ. Продаём то, чем владеем, поэтому
        # выход не считается в обязательства и не отменяется нехваткой денег:
        # отказать себе в выходе значило бы застрять в позиции из-за бюджета.
        if quote['side'] == 'bid':
            if committed + quote['cost'] > budget:
                skipped['бюджет исчерпан'] = skipped.get('бюджет исчерпан', 0) + 1
                continue
            committed += quote['cost']
            entries += 1
        else:
            exits += 1

        maker.place(token, {quote['side']: quote['price'],
                            'size': quote['size'], 'only': quote['side']},
                    top, live)
        placed += 1

    maker.watch_drift(fills, marks)
    drift = maker.measure_drift(marks)
    for row in drift:
        store._append(engine.DRIFT, dict(row, strategy='oneside'))

    report = maker.mark_to_market(marks)
    store._append(EQUITY, {'at': engine._stamp(), **report,
                           'markets': placed, 'fills': len(fills),
                           'entries': entries, 'exits': exits,
                           'drift_measured': len(drift)})
    maker.save()
    return {'placed': placed, 'fills': fills, 'report': report,
            'skipped': skipped, 'entries': entries, 'exits': exits}


def main(loop=False):
    markets = select()
    # СВОЙ КОШЕЛЁК, А НЕ ОБЩИЙ С ДВУСТОРОННЕЙ СХЕМОЙ. Прежде обе спрашивали
    # bankroll_for('MM') и обе считали, что располагают всей суммой: запущенные
    # вместе, они планировали потратить её дважды.
    maker = engine.PaperMaker(bankroll=params.bankroll_for('ONESIDE'),
                              state_path=STATE)
    print(f'капитал: ${maker.bankroll:,.0f}')
    print(f'рынков: {len(markets)}')
    if LAST_PLAN:
        print(f'вложено ${LAST_PLAN["used"]:.2f}, свободно ${LAST_PLAN["free"]:.2f}')
        print(f'ХУДШИЙ СЛУЧАЙ (всё исполнилось и всё против нас): '
              f'-${LAST_PLAN["worst_case_usd"]:.2f}')
        print(f'доступно сделок: {LAST_PLAN["trades_per_hour"]:.0f} в час')
    print('режим: бумага (живого исполнения у этой стратегии нет)')

    while True:
        out = step(maker, markets)
        r = out['report']
        done = [f for f in out['fills']]
        bought = sum(1 for f in done if f['side'] == 'bid')
        sold = sum(1 for f in done if f['side'] == 'ask')
        print(f'[{engine._stamp()}] заявок {out["placed"]} '
              f'(входов {out["entries"]}, выходов {out["exits"]}), '
              f'исполнено {len(done)} (куплено {bought}, продано {sold}), '
              f'капитал ${r["equity"]:,.2f} '
              f'(зафикс ${r["realized"]:+,.2f}, запас ${r["inventory"]:,.2f})')
        if out['skipped']:
            top = sorted(out['skipped'].items(), key=lambda kv: -kv[1])[:3]
            print('   пропущено:', dict(top))
        if not loop:
            return out
        time.sleep(params.MM_POLL_SECONDS)


if __name__ == '__main__':
    main(loop='--loop' in sys.argv)
