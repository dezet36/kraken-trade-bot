"""
Маркет-мейкер: отбор рынков и торговый цикл.

ЧТО ДЕЛАЕТ. Держит двусторонние котировки на многих рынках сразу, ведёт запас и
результат. Работает в бумаге всегда; при явном разрешении дополнительно
отправляет те же заявки на биржу.

БУМАЖНЫЙ РАСЧЁТ НЕ ВЫКЛЮЧАЕТСЯ ДАЖЕ В ЖИВОМ РЕЖИМЕ. Он остаётся моделью, с
которой сверяется реальность: если наши исполнения по ленте расходятся с
настоящими, значит модель очереди ошибается. Узнать это надо раньше, чем
вырастет размер, а не после.

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

ЖИВОЙ РЕЖИМ ТРЕБУЕТ СЕМИ УСЛОВИЙ СРАЗУ (см. executor.can_trade), и ни одно не
выводится из другого: разрешение PM_LIVE, ключ и счёт в окружении, поднявшийся
клиент, отсутствие файла аварийной остановки, дневной убыток ниже предела,
размер заявки под потолком и цена в допустимом диапазоне. Любая одиночная
ошибка — опечатка, забытый флаг, сбой модуля — не приводит к сделке.

Запуск:
    python -m polymarket.mm            один цикл
    python -m polymarket.mm --loop     непрерывно
"""

import json
import sys
import time

from . import book as book_mod
from . import client, engine, executor, params, store, strategy, wallet

CANDIDATES = None          # кэш отбора внутри процесса
LAST_PLAN = {}             # последняя раскладка бюджета, для панели


def select_markets(limit=None, min_liquidity=None, refresh=False, budget=None):
    """
    Рынки под наш капитал. Отбор живёт в selector — здесь только кэш.

    ОТБОР ЗАВИСИТ ОТ РАЗМЕРА СЧЁТА, и это не тонкость. При большом капитале
    доход даёт захват спреда, и чем больше исполнений, тем лучше. При сотне
    долларов наоборот: исполнение приносит запас, который нечем нести — одна
    позиция в $20 это пятая часть счёта. Поэтому рынки берутся те, где мы
    попадём в зачёт награды минимальным размером и где спред уже порога, то
    есть встать у лучшей цены безопасно.
    """
    global CANDIDATES
    if CANDIDATES is not None and not refresh:
        return CANDIDATES
    from . import selector

    money = float(budget if budget is not None else params.bankroll_for('MM'))
    rows = selector.scan(budget=money, limit=limit or params.MM_MARKETS)
    plan = selector.allocate(rows, budget=money)
    CANDIDATES = plan['markets']
    LAST_PLAN.clear()
    LAST_PLAN.update(plan)
    return CANDIDATES


def step(maker, markets, live=False, day_loss=0.0):
    """
    Один проход: стаканы пачкой, исполнения, новые котировки.

    СТАКАНЫ БЕРУТСЯ ОДНИМ ЗАПРОСОМ НА ВСЕ РЫНКИ. Поодиночке двадцать пять
    рынков занимали семь секунд, сотня — почти полминуты, и котировка успевала
    устареть раньше, чем её выставляли. Пачкой те же двадцать пять приходят за
    полсекунды: замерено 0.49 против 7.

    ЖИВОЕ ИСПОЛНЕНИЕ ЗЕРКАЛИТ БУМАЖНОЕ, А НЕ ЗАМЕНЯЕТ ЕГО. Бумажный движок
    считает всегда — он остаётся моделью, с которой сверяется реальность.
    Расхождение между ними и есть самое ценное, что даст живой режим: если наши
    исполнения по ленте не совпадают с настоящими, значит модель очереди
    ошибается, и знать это надо раньше, чем размер вырастет.
    """
    by_token = {m['token_id']: m for m in markets}
    books = book_mod.fetch_many(list(by_token))
    marks, placed, fills, skipped, sent, cancelled = {}, 0, [], {}, 0, 0
    mismatch = None

    for token, market in by_token.items():
        live_book = books.get(str(token))
        if not live_book:
            skipped['стакан не пришёл'] = skipped.get('стакан не пришёл', 0) + 1
            continue
        top = book_mod.top(live_book)
        if not top or top['bid'] is None or top['ask'] is None:
            skipped['стакан односторонний'] = skipped.get('стакан односторонний', 0) + 1
            continue
        marks[str(token)] = top['mid']

    # ЖИВЫЕ СДЕЛКИ И СВЕРКА — ДО ВСЕГО ОСТАЛЬНОГО. Сначала узнаём у биржи, что
    # уже произошло, и только потом решаем, что делать дальше. Обратный порядок
    # считал бы позицию по устаревшим данным.
    if live:
        got = executor.own_trades()
        if got:
            for done in maker.apply_exchange_trades(got):
                store._append(engine.FILLS, done)
                fills.append(done)
        check = executor.reconcile(maker.live_order_ids())
        if check:
            # Призраки — заявки, которые есть у нас и которых нет на бирже.
            # Опаснее сирот: мы считаем, что котируем сторону, а её нет, и
            # перестаём сокращать запас, полагая, что сокращаем.
            if check['ghost']:
                maker.forget_orders(check['ghost'])
            # Сироты — неснятые старые. Снимаем: нас исполнят по цене, которую
            # мы уже забыли.
            for orphan in check['orphan']:
                executor.cancel(orphan)
                cancelled += 1
            mismatch = check

    exposure = maker.exposure(marks)

    for token, market in by_token.items():
        live_book = books.get(str(token))
        if not live_book or str(token) not in marks:
            continue
        top = book_mod.top(live_book)

        # СНАЧАЛА исполнения, потом новые котировки. Обратный порядок выставил
        # бы заявку и тут же проверил её по ленте, где она физически не могла
        # исполниться, — и дал бы себе фору в один цикл.
        slot = maker._slot(token)
        # ИСТОЧНИК ИСПОЛНЕНИЙ ЗАВИСИТ ОТ РЕЖИМА. В бумаге его приходится
        # оценивать по общей ленте и модели очереди — иначе никак. Как только
        # заявки уходят на биржу, оценка становится вредной: она отвечает
        # «исполнилось бы», а биржа знает «исполнилось», и расходятся они
        # обязательно. Живые сделки берутся ниже, одним запросом на все рынки.
        if not live and ((slot.get('orders') or {}).get('bid')
                         or (slot.get('orders') or {}).get('ask')):
            tape = book_mod.tape(market['condition_id'], limit=200)
            for done in maker.process_fills(token, market['condition_id'], tape):
                store._append(engine.FILLS, done)
                fills.append(done)

        quote = strategy.desired_quote(top, market, position=slot['position'],
                                       max_position=params.MM_MAX_POSITION)
        if not quote or quote.get('reason'):
            reason = (quote or {}).get('reason') or 'котировка не собралась'
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        # Потолок вложенного: при превышении котируем ТОЛЬКО сокращающую
        # сторону. Полная остановка была бы хуже — запас остался бы висеть.
        if exposure > params.MM_MAX_EXPOSURE_USD and not quote.get('only'):
            quote['only'] = 'ask' if slot['position'] > 0 else 'bid'

        before = json.dumps(slot.get('orders') or {}, sort_keys=True)
        _, replaced = maker.place(token, quote, top, live_book)
        placed += 1

        # СНАЧАЛА снимаем старое, потом ставим новое. Обратный порядок оставил
        # бы обе заявки в стакане одновременно: двойной размер и двойной риск
        # ровно в тот момент, когда цена уже сдвинулась.
        if live:
            for order_id in replaced:
                executor.cancel(order_id)
                cancelled += 1

        if live and before != json.dumps(slot.get('orders') or {}, sort_keys=True):
            for side in ('bid', 'ask'):
                order = (slot.get('orders') or {}).get(side)
                if not order or order.get('live_id'):
                    continue
                out = executor.place(token, side, order['price'], order['size'],
                                     day_loss_usd=day_loss, tick=market['tick'])
                if out.get('ok'):
                    order['live_id'] = out.get('order_id')
                    sent += 1
                else:
                    order['live_error'] = out.get('why')

    report = maker.mark_to_market(marks)
    store._append(engine.EQUITY, {'at': engine._stamp(), **report,
                                  'markets': placed, 'fills': len(fills),
                                  'sent': sent, 'cancelled': cancelled,
                                  'live': bool(live)})
    maker.save()
    return {'placed': placed, 'fills': fills, 'report': report,
            'skipped': skipped, 'marks': marks, 'sent': sent,
            'cancelled': cancelled, 'mismatch': mismatch}


def main(loop=False):
    markets = select_markets()
    maker = engine.PaperMaker()
    state = wallet.status()
    live = state['can_trade_live']

    print(f'капитал маркет-мейкера: ${maker.bankroll:,.0f}')
    print(f'рынков отобрано: {len(markets)}')
    if LAST_PLAN:
        print(f'вложено ${LAST_PLAN["used"]:,.2f}, свободно ${LAST_PLAN["free"]:,.2f}, '
              f'предел на рынок ${LAST_PLAN["cap_per_market"]:,.2f}')
        print(f'ожидаемая награда: ${LAST_PLAN["expected_daily"]:.3f} в день, '
              f'${LAST_PLAN["expected_monthly"]:.2f} в месяц '
              f'(оценка грубая: доля считается по деньгам в стакане, '
              f'а зачёт идёт по очкам, которых мы не видим)')
    print(f'кошелёк: {"подключён " + str(state["address"]) if state["configured"] else "НЕ подключён"}')
    print(f'режим: {"ЖИВЫЕ ДЕНЬГИ" if live else "бумага"}')
    if state['configured'] and not state['live_enabled']:
        print('   ключ есть, но PM_LIVE не включён — заявки на биржу не уходят')
    if executor.kill_switch_on():
        print('   ВНИМАНИЕ: включена аварийная остановка (файл STOP)')

    try:
        while True:
            report_before = maker.mark_to_market({})
            day_loss = max(0.0, -report_before['pnl'])
            out = step(maker, markets, live=live, day_loss=day_loss)
            r = out['report']
            print(f'[{engine._stamp()}] котировок {out["placed"]}, '
                  f'исполнений {len(out["fills"])}, '
                  f'отправлено {out["sent"]}, снято {out["cancelled"]}, '
                  f'капитал ${r["equity"]:,.2f} '
                  f'(зафикс ${r["realized"]:+,.2f}, запас ${r["inventory"]:,.2f})')
            if out['skipped']:
                print('   пропущено:', dict(out['skipped']))
            if out.get('mismatch') and (out['mismatch']['ghost']
                                        or out['mismatch']['orphan']):
                m = out['mismatch']
                print(f'   СВЕРКА: призраков {len(m["ghost"])}, '
                      f'сирот {len(m["orphan"])} '
                      f'(у нас {m["our_count"]}, на бирже {m["live_count"]})')
            if not loop:
                return out
            time.sleep(params.MM_POLL_SECONDS)
    finally:
        # ЗАЯВКИ СНИМАЮТСЯ ПРИ ЛЮБОМ ВЫХОДЕ, включая аварийный. Оставленные без
        # присмотра — худшее состояние: мы не котируем, но нас продолжают
        # исполнять, причём тогда, когда это выгодно встречной стороне.
        if live:
            print('снимаю заявки с биржи...', executor.cancel_all())


if __name__ == '__main__':
    main(loop='--loop' in sys.argv)
