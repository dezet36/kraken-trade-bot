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

import atexit
import json
import os
import sys
import time

from . import book as book_mod
from . import client, engine, executor, params, store, strategy, wallet

# Мнение модели в живом режиме — рядом с реальностью, а не вместо неё.
SHADOW = os.path.join(store.DIR, 'mm_shadow.jsonl')
# План отбора на диске. Панель живёт в ДРУГОМ процессе и в память
# маркет-мейкера заглянуть не может: без файла она показывала бы прочерки
# вместо названий рынков и «неизвестно» вместо ожидаемого времени круга.
PLAN_FILE = os.path.join(store.DIR, 'mm_plan.json')
# СПРАВОЧНИК ВСЕХ РЫНКОВ, ЧТО МЫ КОГДА-ЛИБО ВЕЛИ. План отбора перезаписывается
# при каждом пересмотре, а позиции остаются — и панель, ища название по токену,
# не находила его и рисовала прочерк. Человек видел открытую позицию без
# единого признака, на каком она рынке.
CATALOGUE = os.path.join(store.DIR, 'mm_markets.json')

# Через сколько замок считается брошенным. Работающий цикл трогает файл каждый
# такт (тридцать секунд), поэтому пять минут — с большим запасом на медленный
# отбор рынков, который занимает до минуты.
_LOCK_STALE_SECONDS = 300

CANDIDATES = None          # кэш отбора внутри процесса
LAST_PLAN = {}             # последняя раскладка бюджета, для панели


def select_markets(limit=None, min_liquidity=None, refresh=False, budget=None):
    """
    Рынки под наш капитал. Отбор живёт в selector — здесь только кэш.

    ОТБОР ЗАВИСИТ ОТ РАЗМЕРА СЧЁТА, и это не тонкость. При большом капитале
    доход даёт захват спреда, и чем больше исполнений, тем лучше. При сотне
    долларов наоборот: исполнение приносит запас, который нечем нести — одна
    позиция в $20 это пятая часть счёта.
    """
    global CANDIDATES
    if CANDIDATES is not None and not refresh:
        return CANDIDATES
    from . import selector

    money = float(budget if budget is not None else params.bankroll_for('MM'))
    rows = selector.scan(budget=money, limit=limit or params.MM_MARKETS)
    plan = selector.allocate(rows, budget=money)
    CANDIDATES = plan['markets']
    remember_markets(rows)
    LAST_PLAN.clear()
    LAST_PLAN.update(plan)
    _save_plan(plan)
    return CANDIDATES


def remember_markets(markets):
    """
    Пополняет справочник: токен → название рынка и его тик.

    Нужен затем, что план отбора живёт ровно до следующего пересмотра, а
    позиция живёт до закрытия. Без справочника панель показывала открытую
    позицию прочерком вместо названия — и понять, где мы стоим, было нельзя.
    """
    try:
        known = {}
        if os.path.exists(CATALOGUE):
            with open(CATALOGUE, encoding='utf-8') as fh:
                known = json.load(fh) or {}
        for market in markets or []:
            token = str(market.get('token_id') or '')
            if not token:
                continue
            known[token] = {'question': market.get('question'),
                            'condition_id': market.get('condition_id'),
                            'token_no': market.get('token_no'),
                            'tick': market.get('tick')}
        os.makedirs(os.path.dirname(CATALOGUE), exist_ok=True)
        with open(CATALOGUE, 'w', encoding='utf-8') as fh:
            json.dump(known, fh, ensure_ascii=False)
    except Exception:                                      # noqa: BLE001
        pass                                               # справочник — удобство


def known_markets():
    """Справочник с диска. Пустой словарь, если его ещё нет."""
    try:
        with open(CATALOGUE, encoding='utf-8') as fh:
            return json.load(fh) or {}
    except Exception:                                      # noqa: BLE001
        return {}


def _save_plan(plan):
    """Кладёт план на диск для панели. Сбой записи не должен ронять отбор."""
    try:
        os.makedirs(os.path.dirname(PLAN_FILE), exist_ok=True)
        slim = [{'token_id': str(m.get('token_id')),
                 'question': m.get('question'),
                 'wait_hours': m.get('wait_hours'),
                 'size': m.get('size'), 'cost': m.get('cost'),
                 'step_ticks': m.get('step_ticks'),
                 'usd_per_hour': m.get('usd_per_hour')}
                for m in plan.get('markets') or []]
        with open(PLAN_FILE, 'w', encoding='utf-8') as fh:
            json.dump({'at': engine._stamp(), 'used': plan.get('used'),
                       'free': plan.get('free'), 'markets': slim},
                      fh, ensure_ascii=False)
    except Exception:                                      # noqa: BLE001
        pass


def rotate(maker, current, budget=None):
    """
    Пересматривает список рынков: уходит из затихших, входит в новые.

    ПРЕЖДЕ СПИСОК ЗАМОРАЖИВАЛСЯ ПРИ ЗАПУСКЕ И НЕ МЕНЯЛСЯ НИКОГДА. Бот не видел
    ни новых событий, ни того, что рынок затих: выбрав двадцать рынков в
    понедельник, он стоял в них и в пятницу, даже если торговля там кончилась.
    При сотне долларов это не мелочь — каждые $5 в мёртвом рынке есть
    двадцатая часть счёта, не работающая вовсе.

    ДВА ПРАВИЛА, И ОБА НЕСИММЕТРИЧНЫ.

    Рынок с ОТКРЫТЫМ ЗАПАСОМ не бросается никогда, как бы он ни затих. Уйти,
    оставив позицию, значит перестать ею управлять: наклон котировки — наш
    единственный способ разгрузиться, и он работает только пока мы котируем.

    Рынок БЕЗ запаса и БЕЗ единого исполнения за отведённые часы уступает
    место следующему кандидату. Ошибиться здесь дёшево: мы ничего не теряем,
    кроме места в очереди, которое всё равно ничего не принесло.
    """
    fresh = select_markets(refresh=True, budget=budget)
    by_token = {m['token_id']: m for m in fresh}
    now = time.time()
    keep, dropped = [], []
    for market in current:
        slot = maker.state['books'].get(str(market['token_id'])) or {}
        if slot.get('position'):
            keep.append(by_token.get(market['token_id'], market))
            continue
        # СРОК ОЖИДАНИЯ СЧИТАЕТСЯ ОТ ОБЕЩАНИЯ РЫНКА, А НЕ ОДИН НА ВСЕХ.
        #
        # Шесть часов на всё подряд — это ровно та жалоба, с которой пришёл
        # человек: «бот висит часами на одном рынке». Рынок, которому отбор
        # обещал круг за двадцать минут, к шестому часу ошибся в восемнадцать
        # раз, и ждать его дальше незачем: место в списке стоит дороже.
        #
        # Берём тройное обещание, но не дольше общего потолка. Тройка — не
        # красивое число: одинарного мало (очередь плавает, и мы бы дёргались
        # на шуме), а к тройному сроку ошибка модели уже не случайность.
        promised = market.get('wait_hours')
        patience = params.MM_IDLE_HOURS * 3600
        if promised not in (None, float('inf')) and promised > 0:
            patience = min(patience, float(promised) * 3600 * params.MM_IDLE_TRIES)
        idle = now - float(market.get('joined_ts') or now)
        if not slot.get('trades') and idle > patience:
            dropped.append(market)
            continue
        keep.append(by_token.get(market['token_id'], market))

    # Место освободилось — добираем лучших из новых, кого ещё нет в работе.
    # Список отсортирован по ожидаемому доходу в час, поэтому «лучшие» это
    # просто первые.
    #
    # ТОЛЬКО ЧТО ПОКИНУТЫЕ РЫНКИ ИСКЛЮЧАЮТСЯ, иначе пересмотр вырождается: они
    # остаются добротными кандидатами по всем меркам отбора и возвращаются
    # первыми же, а мы обнуляем им отсчёт простоя и стоим там вечно. Поймано
    # тестом, а не рассуждением.
    have = {m['token_id'] for m in keep} | {m['token_id'] for m in dropped}
    for market in fresh:
        if len(keep) >= len(current):
            break
        if market['token_id'] in have:
            continue
        market['joined_ts'] = now
        keep.append(market)
        have.add(market['token_id'])
    for market in keep:
        market.setdefault('joined_ts', now)
    return keep, dropped


def as_our_side(trades, markets):
    """
    Сделки встречного токена, приведённые к нашей стороне учёта.

    ЗАЧЕМ ЭТО НУЖНО. Продажа «ДА» уходит на биржу покупкой «НЕТ» — иначе биржа
    её не примет вовсе. Обратно она приходит сделкой ПО ДРУГОМУ ТОКЕНУ, и без
    перевода учёт завёл бы вторую позицию вместо того, чтобы сократить первую:
    два разных токена вместо одного рынка.

    Перевод тот же, что и для ленты: цена зеркалится, сторона переворачивается,
    размер остаётся. Контракт «нет» и контракт «да» — одна и та же единица.
    """
    twins = {}
    for market in markets or []:
        twin = market.get('token_no')
        if twin:
            twins[str(twin)] = str(market['token_id'])
    out = []
    for trade in trades or []:
        ours = twins.get(str(trade.get('token')))
        if not ours:
            out.append(trade)
            continue
        out.append({**trade, 'token': ours,
                    'side': 'ask' if trade['side'] == 'bid' else 'bid',
                    'price': round(1.0 - float(trade['price']), 10),
                    'mirrored': True})
    return out


def step(maker, markets, live=False, day_loss=0.0, deadline=None):
    """
    Один проход: стаканы пачкой, исполнения, новые котировки.

    СТАКАНЫ БЕРУТСЯ ОДНИМ ЗАПРОСОМ НА ВСЕ РЫНКИ. Поодиночке двадцать пять
    рынков занимали семь секунд, сотня — почти полминуты, и котировка успевала
    устареть раньше, чем её выставляли. Пачкой те же двадцать пять приходят за
    полсекунды: замерено 0.49 против 7.

    У ТАКТА ЕСТЬ СРОК, И ЭТО НЕ ПЕРЕСТРАХОВКА. Каждый рынок стоит нам
    нескольких обращений к бирже, а у каждого обращения свой предел в пять
    секунд. Десяток рынков с медленно отвечающей биржей — и такт растягивается
    на минуты: счётчик циклов стоит, панель показывает ноль, снаружи бот
    выглядит мёртвым. Наблюдалось на живом счёте при перебоях у площадки.
    Дойдя до срока, доделываем текущий рынок и выходим: лучше обойти половину
    списка вовремя, чем весь список неизвестно когда.

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
            # СДЕЛКИ ВСТРЕЧНОГО ТОКЕНА ПЕРЕВОДЯТСЯ В НАШУ СТОРОНУ. Без этого
            # исполненная продажа приходила бы обратно ПОКУПКОЙ ДРУГОГО токена
            # и заводила бы вторую позицию вместо сокращения первой.
            got = as_our_side(got, markets)
            for done in maker.apply_exchange_trades(got):
                # КОНТЕКСТ КЛАДЁТСЯ РЯДОМ С ИСПОЛНЕНИЕМ, а не добывается потом.
                # Разбирать статистику по рынкам, имея один номер токена,
                # нельзя: к моменту разбора план успевает смениться, и
                # название рынка взять уже неоткуда.
                market = by_token.get(str(done.get('token'))) or {}
                done['question'] = market.get('question')
                done['condition'] = market.get('condition_id')
                done['planned_gain'] = market.get('our_gain')
                done['planned_wait_min'] = (
                    round(market['wait_hours'] * 60)
                    if market.get('wait_hours') not in (None, float('inf'))
                    else None)
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

    # ЛЕНТЫ БЕРУТСЯ ОДНИМ ПАКЕТОМ НА ВЕСЬ ТАКТ, А НЕ ПО ОДНОЙ НА РЫНОК.
    #
    # Здесь стоял отдельный сетевой запрос внутри цикла по рынкам: десять
    # рынков — десять запросов, и каждый со своей задержкой. Такт растягивался
    # на минуты, а котировки за это время устаревали. Ровно эта же ошибка уже
    # исправлялась в отборе — там пакетная выборка сократила семь минут до
    # сорока восьми секунд, — но в торговом цикле осталась.
    #
    # Берём только те рынки, где у нас есть заявки: остальным лента не нужна.
    need_tape = [m['condition_id'] for token, m in by_token.items()
                 if ((maker._slot(token).get('orders') or {}).get('bid')
                     or (maker._slot(token).get('orders') or {}).get('ask'))]
    tapes = book_mod.tape_many(need_tape) if need_tape else {}

    exposure = maker.exposure(marks)

    # СРОК УДЕРЖАНИЯ ПРОВЕРЯЕТСЯ КАЖДЫЙ ТАКТ, и до сих пор он не проверялся
    # ВООБЩЕ. Ограничение записано в заголовке модуля с самого начала —
    # «застрявшая позиция превращает мейкера в предсказателя», — а функция
    # stale_positions существовала и не вызывалась ни разу. Позиция могла
    # висеть сутками, держа деньги, которые должны работать на других рынках.
    stale = set(maker.stale_positions())

    # ПОЗИЦИЯ, УШЕДШАЯ СЛИШКОМ ДАЛЕКО, ЗАКРЫВАЕТСЯ ТАК ЖЕ, КАК ПРОСРОЧЕННАЯ.
    #
    # Срок ловит застой, а это ловит движение. Замерено на двенадцати живых
    # позициях: настоящий убыток $1.56, и $0.94 из него сделала одна — рынок
    # переоценился с 0.637 до 0.450 за считанные часы. Одиннадцать остальных
    # вместе дали $0.62. Без предела одна такая позиция стоит дороже, чем
    # приносят все удачные круги за сутки.
    hurt = set()
    for token, slot in (maker.state.get('books') or {}).items():
        position = float(slot.get('position') or 0)
        cost = float(slot.get('avg_cost') or 0)
        mark = marks.get(str(token))
        if not position or not cost or mark is None:
            continue
        loss = (mark - cost) / cost if position > 0 else (cost - mark) / cost
        if loss <= -params.MM_MAX_POSITION_LOSS:
            hurt.add(str(token))
    if hurt:
        from logger import log as _hurt_say
        _hurt_say(f'◈ Polymarket: {len(hurt)} позиций ушли дальше '
                  f'{params.MM_MAX_POSITION_LOSS:.0%} — закрываю, не доливаю')
    if stale:
        from logger import log as _say
        _say(f'◈ Polymarket: {len(stale)} позиций держатся дольше '
             f'{params.MM_MAX_HOLD_HOURS:.0f} ч — закрываю по лучшей цене')

    # ОБЯЗАТЕЛЬСТВА СЧИТАЮТСЯ ПРОТИВ ДЕНЕГ, А НЕ ПРОТИВ ОТДЕЛЬНОГО ПОТОЛКА.
    # Двусторонняя котировка стоит РОВНО размер при любой цене (покупка берёт
    # p, продажа — (1-p)), поэтому обещанное складывается просто. Без этого
    # счётчика бот выставлял на 90 рынках по 100-200 контрактов — около $9 000
    # обещаний при $450 денег. Каждая заявка в отдельности выглядела скромно;
    # опасна была только сумма, а сумму никто не считал.
    committed = 0.0
    budget = float(maker.state['cash'])

    # В ЖИВОМ РЕЖИМЕ ПОТОЛОК СТАВИТ БИРЖА, А НЕ НАШ СЧЁТ НА БУМАГЕ.
    #
    # Бумажные деньги считают, сколько мы СОБИРАЛИСЬ вложить; биржа знает,
    # сколько свободно на самом деле. Разойтись они обязаны: часть денег уже
    # заперта под стоящими заявками, часть ушла в исполненную покупку. Пока
    # ориентиром была бумага, бот раз за разом слал заявки, на которые денег
    # нет, и получал «not enough balance» пачками — по строке на каждый рынок
    # каждый такт. Наблюдалось на живом счёте: восемь рынков, поток отказов,
    # и ни одного слова о том, что деньги просто кончились.
    if live:
        try:
            free = wallet.balance()
            if free is not None:
                budget = min(budget, float(free))
        except Exception:                                   # noqa: BLE001
            pass                                            # не спросили — идём по бумаге

    ran_out = 0
    for token, market in by_token.items():
        if deadline and time.time() > deadline:
            ran_out += 1
            continue
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
        has_orders = ((slot.get('orders') or {}).get('bid')
                      or (slot.get('orders') or {}).get('ask'))
        if has_orders:
            tape = tapes.get(market['condition_id']) or []
            if live:
                # МОДЕЛЬ ИДЁТ РЯДОМ, А НЕ ВМЕСТО. В живом режиме позиции и
                # деньги ведёт биржа; модель отвечает на свой вопрос и
                # оставляет мнение в стороне. Их расхождение и есть самое
                # ценное, что даст живой прогон: оно скажет, насколько можно
                # верить бумаге, когда придёт время увеличивать размер.
                #
                # Прежде эта ветка просто пропускалась, и сравнивать было
                # нечего — при том что заголовок модуля обещал обратное.
                guess = maker.predict_fills(token, tape)
                if guess:
                    store._append(SHADOW, {
                        'at': engine._stamp(), 'token': str(token),
                        'question': market.get('question'), 'sides': guess})
            else:
                for done in maker.process_fills(token, market['condition_id'],
                                                tape):
                    store._append(engine.FILLS, done)
                    fills.append(done)

        # Признак просрочки едет в котировку: решает её стратегия, а знает о
        # сроке — состояние. Смешивать эти два знания в одном месте значило бы
        # тащить время жизни позиции в чистую функцию котирования.
        market = dict(market,
                      stale=str(token) in stale or str(token) in hurt)
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

        # ДЕНЬГИ КОНЧИЛИСЬ — ДАЛЬШЕ НЕ КОТИРУЕМ. Рынки идут по убыванию
        # оборота, поэтому обрезается хвост, а не середина. Сокращающую
        # сторону оставляем: она не тратит денег, а высвобождает их.
        need = float(quote['size']) if not quote.get('only') else 0.0
        if committed + need > budget:
            if slot['position']:
                quote['only'] = 'ask' if slot['position'] > 0 else 'bid'
            else:
                skipped['бюджет исчерпан'] = skipped.get('бюджет исчерпан', 0) + 1
                continue
        committed += need

        before = json.dumps(slot.get('orders') or {}, sort_keys=True)
        _, replaced = maker.place(token, quote, top, live_book,
                                  market_tick=market.get('tick'))
        placed += 1

        # СНАЧАЛА снимаем старое, потом ставим новое. Обратный порядок оставил
        # бы обе заявки в стакане одновременно: двойной размер и двойной риск
        # ровно в тот момент, когда цена уже сдвинулась.
        if live:
            for order_id in replaced:
                executor.cancel(order_id)
                cancelled += 1

        # ОТПРАВЛЯЕМ ВСЁ, У ЧЕГО НЕТ БИРЖЕВОГО НОМЕРА, а не только изменившееся.
        #
        # Здесь стояло условие «котировка изменилась с прошлого такта», и оно
        # ломало ровно тот порядок действий, который человек и выбирает:
        # запустил маркет-мейкер, потом подключил кошелёк, потом включил живую
        # торговлю. Котировки к тому моменту уже выставлены и с места не
        # двигаются, пока не двинется рынок, — а значит не отправляются НИКОГДА.
        # Поймано на живом счёте: поток перешёл в живой режим, отработал шесть
        # тактов, на бирже ноль заявок.
        #
        # От повторной отправки защищает сам номер: он появляется у заявки
        # после успешной отправки, и второй раз она уже не уходит. А отказ
        # биржи имеет смысл повторить — причина бывает временной.
        if live:
            for side in ('bid', 'ask'):
                order = (slot.get('orders') or {}).get(side)
                if not order or order.get('live_id'):
                    continue
                if order.get('retry_at') and time.time() < order['retry_at']:
                    continue
                # ВСТРЕЧНЫЙ ТОКЕН И ЗАПАС ПЕРЕДАЮТСЯ ОБЯЗАТЕЛЬНО. Без них
                # продажа уходит на биржу как есть — и отвергается, потому
                # что продавать нечего. Так и было: на Polymarket стояли одни
                # покупки, а маркет-мейкинга не было вовсе.
                out = executor.place(token, side, order['price'], order['size'],
                                     day_loss_usd=day_loss, tick=market['tick'],
                                     twin_token=market.get('token_no'),
                                     holding=slot['position'])
                # ОТВЕРГНУТАЯ ЗАЯВКА ПОВТОРЯЕТСЯ С ОТСТУПОМ, А НЕ КАЖДЫЙ ТАКТ.
                #
                # Причина отказа чаще всего не меняется за тридцать секунд:
                # не тот счёт, не хватает денег, рынок закрылся. Повторяя
                # каждый такт, мы тратили на безнадёжные отправки всё время
                # цикла — в журнале за сутки 2480 ошибок против 125 удачных
                # заявок. Отступ удваивается, поэтому безнадёжное затихает
                # само, а временное восстановится на следующей попытке.
                if not out.get('ok'):
                    wait = min(float(order.get('retry_wait') or
                                     params.MM_RETRY_SECONDS),
                               params.MM_RETRY_MAX_SECONDS)
                    order['retry_wait'] = wait * 2
                    order['retry_at'] = time.time() + wait
                if out.get('ok'):
                    order['live_id'] = out.get('order_id')
                    order.pop('retry_at', None)
                    order.pop('retry_wait', None)
                    # ПРЕЖНЯЯ ОШИБКА СТИРАЕТСЯ. Без этого строка в панели вечно
                    # показывала отказ, которого уже нет: заявка стоит на
                    # бирже, а рядом с ней висит текст прошлой неудачи.
                    order.pop('live_error', None)
                    sent += 1
                else:
                    order['live_error'] = out.get('why')

    # СНОС ЦЕНЫ ПОСЛЕ НАШИХ ИСПОЛНЕНИЙ. Ставится на учёт здесь, а снимается
    # через полчаса — то есть отвечает не «сколько мы взяли спреда», а «сколько
    # его отобрали обратно». Без этого числа все расчёты доходности остаются
    # потолком: спред виден заранее, а неблагоприятный отбор — только так.
    maker.watch_drift(fills, marks)
    drift = maker.measure_drift(marks)
    for row in drift:
        store._append(engine.DRIFT, row)

    report = maker.mark_to_market(marks)
    store._append(engine.EQUITY, {'at': engine._stamp(), **report,
                                  'markets': placed, 'fills': len(fills),
                                  'sent': sent, 'cancelled': cancelled,
                                  'drift_measured': len(drift),
                                  'drift_pending': len(maker.state.get('drift_pending', [])),
                                  'live': bool(live)})
    maker.save()
    if ran_out:
        skipped['не успели за такт'] = ran_out
    return {'placed': placed, 'fills': fills, 'report': report,
            'skipped': skipped, 'marks': marks, 'sent': sent,
            'cancelled': cancelled, 'mismatch': mismatch,
            'ran_out': ran_out}


def _single_instance():
    """
    Не даёт запустить второго маркет-мейкера поверх первого.

    ПОЙМАНО НА СЕБЕ, И ИМЕННО ПОЭТОМУ ЗАМОК ЗДЕСЬ. Три процесса разом писали в
    одно состояние: один со старым кодом в памяти, два с новым. Состояние
    вышло смешанным — заявки по 5, 100 и 200 контрактов вперемешку, число
    рынков скакало 46, 90, 48 от цикла к циклу. В бумаге это путаница; на
    бирже это двойные заявки, двойной запас и снятие чужих номеров.

    Замок — файл с номером процесса. Мёртвый номер не считается: иначе после
    аварийного завершения бот больше не запустился бы никогда.
    """
    # ПАПКА СОЗДАЁТСЯ ПЕРЕД ЗАМКОМ, и это не мелочь: на свежей установке её
    # нет вовсе, и запись замка падала с «нет такого файла» — ПЕРВОЙ же
    # строкой, до всякой торговли. Снаружи это выглядело как «нажимаю
    # Запустить, и ничего не происходит»: служба умирала раньше, чем успевала
    # что-либо сказать, а кнопка возвращалась в исходное состояние.
    os.makedirs(store.DIR, exist_ok=True)

    path = os.path.join(store.DIR, 'mm.lock')
    if os.path.exists(path):
        try:
            older = int(open(path, encoding='utf-8').read().strip())
        except Exception:                                  # noqa: BLE001
            older = None
        # ЗАМОК ПРОТУХАЕТ ПО ВРЕМЕНИ, А НЕ ТОЛЬКО ПО СМЕРТИ ПРОЦЕССА.
        #
        # Номера процессов ПЕРЕИСПОЛЬЗУЮТСЯ. Достаточно, чтобы после
        # аварийного завершения система выдала тот же номер чему угодно
        # постороннему — и «процесс жив» станет правдой навсегда, а
        # маркет-мейкер не запустится больше никогда, причём с сообщением о
        # чужой копии, которой нет. Убрать замок из панели нельзя: он лежит
        # файлом рядом с данными.
        #
        # Работающий цикл трогает файл каждый такт. Нетронутый дольше срока —
        # брошенный, кто бы ни носил сейчас его номер.
        stale = (time.time() - os.path.getmtime(path)) > _LOCK_STALE_SECONDS
        if older and older != os.getpid() and _alive(older) and not stale:
            print(f'уже работает маркет-мейкер (процесс {older}). '
                  f'Второй запуск отменён: два процесса пишут в одно '
                  f'состояние и мешают друг другу.')
            return False
        if stale:
            print('замок остался от прежнего запуска и протух — забираю')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    return True


def _touch_lock():
    """
    Отмечает, что цикл жив. По этой отметке и протухает замок.

    Без неё замок «жив, пока жив процесс», а номера процессов
    переиспользуются: чужой процесс с тем же номером запирал бы запуск
    навсегда.
    """
    try:
        os.utime(os.path.join(store.DIR, 'mm.lock'), None)
    except Exception:                                      # noqa: BLE001
        pass


def _alive(pid):
    """Жив ли процесс. Мёртвый замок обязан сниматься сам."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:                                      # noqa: BLE001
        return True
    return True


def main(loop=False):
    if not _single_instance():
        return
    markets = select_markets()
    maker = engine.PaperMaker()
    state = wallet.status()
    live = state['can_trade_live']

    # ОТКУДА ВЗЯЛАСЬ СУММА — ПЕЧАТАЕТСЯ ВСЕГДА. Бюджет отделяет «бот работает»
    # от «бот стоит», и молчаливый ноль здесь неотличим от поломки: заявок нет,
    # ошибок нет, причины нет. Настройка может значить «сто долларов», «всё с
    # кошелька» или «доля остатка» — какое из трёх сработало, видно только тут.
    money, why = params.budget_plan('MM')
    print(f'капитал маркет-мейкера: ${maker.bankroll:,.2f}  ({why})')
    if maker.budget_note:
        print(f'   {maker.budget_note}')
    if money <= 0:
        print('   БЮДЖЕТ НУЛЕВОЙ — заявки выставляться не будут')
    print(f'рынков отобрано: {len(markets)}')
    if LAST_PLAN:
        print(f'вложено ${LAST_PLAN["used"]:,.2f}, свободно ${LAST_PLAN["free"]:,.2f}, '
              f'предел на рынок ${LAST_PLAN["cap_per_market"]:,.2f}')
        print(f'потолок за оборот всех рынков: '
              f'${LAST_PLAN["ceiling_per_round_usd"]:.2f} — это ПОТОЛОК, а не '
              f'ожидание: он предполагает, что обе стороны исполнились по '
              f'нашим ценам и ни разу не против нас')
        print(f'награда сверх: ${LAST_PLAN["rewards_monthly"]:.2f} в месяц')
    print(f'кошелёк: {"подключён " + str(state["address"]) if state["configured"] else "НЕ подключён"}')
    print(f'режим: {"ЖИВЫЕ ДЕНЬГИ" if live else "бумага"}')
    if state['configured'] and not state['live_enabled']:
        print('   ключ есть, но PM_LIVE не включён — заявки на биржу не уходят')
    if executor.kill_switch_on():
        print('   ВНИМАНИЕ: включена аварийная остановка (файл STOP)')

    last_scan = time.time()
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
            _touch_lock()
            if not loop:
                return out

            # ПЕРЕСМОТР СПИСКА РЫНКОВ. Прежде список замерзал при запуске:
            # выбрав рынки в понедельник, бот стоял в них и в пятницу, даже
            # если торговля там кончилась. Новые события он не видел вовсе.
            if time.time() - last_scan > params.MM_RESCAN_MINUTES * 60:
                markets, left = rotate(maker, markets)
                last_scan = time.time()
                if left:
                    # ЗАЯВКИ НА ПОКИДАЕМЫХ РЫНКАХ СНИМАЮТСЯ ДО УХОДА. Уйти,
                    # оставив их в стакане, значит перестать за ними следить,
                    # продолжая быть исполняемым, — и исполнят нас тогда, когда
                    # это выгодно встречной стороне.
                    for market in left:
                        slot = maker.state['books'].get(str(market['token_id']))
                        for side in ('bid', 'ask'):
                            order = ((slot or {}).get('orders') or {}).get(side)
                            if live and order and order.get('live_id'):
                                executor.cancel(order['live_id'])
                            if slot:
                                slot.setdefault('orders', {})[side] = None
                    print(f'   пересмотр: ушли с {len(left)}, '
                          f'работаем на {len(markets)}')
            time.sleep(params.MM_POLL_SECONDS)
    finally:
        # ЗАЯВКИ СНИМАЮТСЯ ПРИ ЛЮБОМ ВЫХОДЕ, включая аварийный. Оставленные без
        # присмотра — худшее состояние: мы не котируем, но нас продолжают
        # исполнять, причём тогда, когда это выгодно встречной стороне.
        if live:
            print('снимаю заявки с биржи...', executor.cancel_all())


if __name__ == '__main__':
    main(loop='--loop' in sys.argv)
