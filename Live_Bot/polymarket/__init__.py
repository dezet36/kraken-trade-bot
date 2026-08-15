"""
Polymarket: доступ к площадке, сбор данных и сигнальные модули.

ПАКЕТ НЕЗАВИСИМ ОТ ТОРГОВЫХ СТРАТЕГИЙ БОТА, и это правило проекта, а не
удобство. FIBO, SMC, LEVELS и RSIBB торгуют бессрочные фьючерсы на бирже; здесь
другая площадка, другие издержки, другой способ разрешения сделки. Общий код
между ними означал бы, что правка ради одного молча меняет другое.

Состав:
    params   — пороги, лимиты, станции; всё через переменные окружения
    client   — Gamma и CLOB, с обходом ловушек, найденных замерами
    store    — накопление снимков цен: истории у площадки нет, копим свою
    weather  — распределение максимума температуры по станции
    longshot — продажа дешёвых лонгшотов; ВАЛИДАЦИЯ ПРОВАЛЕНА, см. модуль
    risk     — размер ставки, лимиты, аварийная остановка
"""

__all__ = ['params', 'client', 'store', 'weather', 'longshot', 'risk']



def _connect_state():
    """Состояние кошелька для панели. Сбой здесь не должен ронять весь снимок."""
    try:
        from . import connect
        return connect.state()
    except Exception:                                       # noqa: BLE001
        return None


_EXCHANGE_CACHE = {'at': 0.0, 'view': None}
_EXCHANGE_TTL = 20          # секунд; панель опрашивается чаще, биржа — нет


def exchange_view(force=False):
    """
    ЧТО ВИДИТ САМА БИРЖА: остаток, стоящие заявки, сделки.

    ЗАЧЕМ ЭТО ОТДЕЛЬНО ОТ ВСЕГО ОСТАЛЬНОГО. Панель до сих пор показывала
    БУМАЖНУЮ модель — и показывала бы ровно то же самое, даже если бы ни одна
    заявка до биржи не дошла. Человек смотрел на «стоим на трёх рынках»,
    открывал Polymarket, не видел там ничего и не мог понять, кто из двоих
    врёт. Правильный ответ — спросить биржу и показать её ответ рядом, назвав
    вещи своими именами: слева факт, справа расчёт.

    Ответ кэшируется: панель обновляется каждые несколько секунд, а три
    сетевых запроса на каждое обновление — это лишняя нагрузка и на биржу, и
    на панель.
    """
    import time as _time

    fresh = _EXCHANGE_CACHE['view']
    if fresh and not force and _time.time() - _EXCHANGE_CACHE['at'] < _EXCHANGE_TTL:
        return fresh

    from . import engine, executor, wallet

    state = wallet.status()
    view = {'at': engine._stamp(), 'asked': False,
            'signer': state.get('address'), 'funder': state.get('funder'),
            'balance': None, 'orders': None, 'trades': None, 'why': ''}

    if not state['configured']:
        view['why'] = 'кошелёк не подключён — спрашивать биржу не о чем'
        _EXCHANGE_CACHE.update({'at': _time.time(), 'view': view})
        return view

    if wallet.client() is None:
        view['why'] = wallet.explain_failure() or 'торговый клиент не поднялся'
        _EXCHANGE_CACHE.update({'at': _time.time(), 'view': view})
        return view

    view['balance'] = wallet.balance()
    rows = executor.open_orders()
    if rows is not None:
        view['orders'] = len(rows)
        view['orders_detail'] = [{
            'token': str(r.get('asset_id') or ''),
            'side': 'bid' if str(r.get('side', '')).upper() == 'BUY' else 'ask',
            'price': r.get('price'),
            'size': r.get('original_size'),
            'matched': r.get('size_matched'),
        } for r in rows[:20]]
    done = executor.own_trades()
    if done is not None:
        view['trades'] = len(done)
        if done:
            view.update(_holdings_value(done))
        elif fresh:
            # ПУСТОЙ ОТВЕТ НЕ ЗНАЧИТ «ПОЗИЦИЙ НЕТ». Биржа иногда отдаёт пустой
            # список сделок на ровном месте, и панель показывала «токены $0.00»
            # при тринадцати живых позициях — то есть счёт выглядел на двадцать
            # долларов беднее, чем есть. Держим прежнюю оценку, пока не придёт
            # непустой ответ.
            for key in ('positions_value', 'positions_paid', 'positions_count'):
                if key in fresh:
                    view[key] = fresh[key]
    view['asked'] = True
    _EXCHANGE_CACHE.update({'at': _time.time(), 'view': view})
    return view


def _stats_safely():
    """Сводка по журналам. Её отсутствие не должно ронять весь снимок."""
    try:
        from . import stats
        return stats.report()
    except Exception as exc:                                # noqa: BLE001
        return {'error': f'{type(exc).__name__}: {str(exc)[:120]}'}


def _holdings_value(trades):
    """
    Токены на руках и что за них дадут ПРЯМО СЕЙЧАС.

    ПОЧЕМУ ПО БИДУ, А НЕ ПО СЕРЕДИНЕ. Середина — это цена, по которой никто не
    обязан у нас покупать; бид — та, по которой купят сегодня. Разница выглядит
    мелочью, пока позиций одна, и становится всем результатом, когда их
    двенадцать: панель показывала -$0.04, а на счёте было -$2.89, и вся разница
    сидела ровно в этом выборе цены.

    Оценка по середине к тому же льстит систематически: нас исполняют, когда
    цена идёт против нас, поэтому наш запас почти всегда стоит ближе к биду.
    """
    from collections import defaultdict

    from . import book as book_mod

    held = defaultdict(float)
    paid = 0.0
    for trade in trades or []:
        size = float(trade.get('size') or 0)
        price = float(trade.get('price') or 0)
        if trade.get('side') == 'bid':
            held[str(trade.get('token'))] += size
            paid += price * size
        else:
            held[str(trade.get('token'))] -= size
            paid -= price * size
    held = {k: v for k, v in held.items() if abs(v) > 1e-9}
    if not held:
        return {'positions_value': 0.0, 'positions_paid': round(paid, 4),
                'positions_count': 0}
    try:
        books = book_mod.fetch_many(list(held))
    except Exception:                                       # noqa: BLE001
        return {'positions_value': None, 'positions_paid': round(paid, 4),
                'positions_count': len(held)}
    value = 0.0
    for token, qty in held.items():
        live = books.get(str(token))
        top = book_mod.top(live) if live else None
        price = (top or {}).get('bid') or 0.0
        value += qty * float(price)
    return {'positions_value': round(value, 4),
            'positions_paid': round(paid, 4),
            'positions_count': len(held)}


def _exchange_safely():
    """Опрос биржи не должен ронять весь снимок: сеть бывает недоступна."""
    try:
        return exchange_view()
    except Exception as exc:                                # noqa: BLE001
        return {'asked': False, 'why': f'{type(exc).__name__}: {str(exc)[:120]}'}


def _standing_quotes(books, planned, catalogue=None):
    """
    Где бот стоит сейчас: цены, сторона, сколько ждёт и чего ждёт.

    БОЛЬШУЮ ЧАСТЬ ВРЕМЕНИ МЕЙКЕР ИМЕННО СТОИТ, и это его основная работа.
    Панель показывала только позиции — то есть рынки, где нас уже исполнили, —
    и на вопрос «чем он занят» отвечала пустой таблицей при девяти работающих
    рынках.

    Ожидание берётся из плана отбора: там оно посчитано по очереди и потоку.
    Сколько заявка стоит на деле — считается по времени выставления. Расхождение
    этих двух чисел и есть первый признак, что модель врёт.
    """
    import time as _time
    # СПРАВОЧНИК ПОДСТРАХОВЫВАЕТ ПЛАН. План отбора перезаписывается при каждом
    # пересмотре, а позиция живёт до закрытия — и рынок, где мы стоим, из плана
    # исчезает. Панель показывала такую строку прочерком вместо названия:
    # открытая позиция без единого признака, на каком она рынке. Замерено на
    # живом счёте: десять позиций из десяти оказались вне плана.
    by_token = {str(t): dict(m or {}) for t, m in (catalogue or {}).items()}
    for m in planned:
        token = str(m.get('token_id'))
        by_token.setdefault(token, {}).update(m)
    now = _time.time()
    rows = []
    for token, slot in (books or {}).items():
        orders = slot.get('orders') or {}
        bid, ask = orders.get('bid'), orders.get('ask')
        if not bid and not ask:
            continue
        market = by_token.get(str(token)) or {}
        oldest = min([o['ts'] for o in (bid, ask) if o] or [now])
        keep = (ask['price'] - bid['price']) if (bid and ask) else None
        # ДОШЛА ЛИ ЗАЯВКА ДО БИРЖИ. Номер от биржи есть — значит она её
        # приняла; есть текст ошибки — значит отвергла, и причину надо
        # показать. Прежде обе эти вещи записывались и не читались никем:
        # строка выглядела одинаково и когда заявка стоит на Polymarket, и
        # когда она не ушла туда вовсе.
        sent = [o for o in (bid, ask) if o and o.get('live_id')]
        failed = [o.get('live_error') for o in (bid, ask)
                  if o and o.get('live_error')]
        rows.append({
            'live_ids': len(sent),
            'live_error': failed[0] if failed else None,
            'token': str(token),
            'question': market.get('question') or '—',
            'bid': bid['price'] if bid else None,
            'ask': ask['price'] if ask else None,
            'size': (bid or ask).get('size'),
            'spread_kept': round(keep, 4) if keep is not None else None,
            'queue_bid': (bid or {}).get('queue'),
            'queue_ask': (ask or {}).get('queue'),
            'standing_min': round(max(0.0, now - oldest) / 60, 1),
            'expected_min': (round(market['wait_hours'] * 60)
                             if market.get('wait_hours') not in (None, float('inf'))
                             else None),
            'position': slot.get('position') or 0.0,
        })
    # Дольше всех стоящие — наверх: именно они вызывают вопрос, а не те, что
    # выставлены минуту назад.
    rows.sort(key=lambda r: -r['standing_min'])
    return rows


def _round_trips(fills):
    """
    Завершённые круги: вход, выход, заработок, длительность.

    ГЛАВНАЯ МЕРКА РАБОТЫ МЕЙКЕРА. Отдельные исполнения не говорят ничего:
    купить может каждый, заработок появляется только при закрытии. Круг
    считается по паре исполнений на одном рынке в противоположные стороны.

    Пары собираются в порядке журнала: первая покупка закрывается первой
    продажей. Это не «первым пришёл — первым ушёл» в бухгалтерском смысле —
    учёт ведётся по средней цене и лежит в состоянии; здесь только показ.
    """
    from collections import defaultdict
    import time as _time
    waiting = defaultdict(list)
    rounds = []
    for fill in fills or []:
        token = str(fill.get('token'))
        side = fill.get('side')
        opposite = 'ask' if side == 'bid' else 'bid'
        queue = waiting[(token, opposite)]
        if queue:
            first = queue.pop(0)
            entry, exit_ = (first, fill) if first['side'] == 'bid' \
                else (fill, first)
            size = min(entry.get('size') or 0, exit_.get('size') or 0)
            gain = (exit_['price'] - entry['price']) * size
            try:
                seconds = abs(_time.mktime(_time.strptime(
                    exit_['at'], '%Y-%m-%dT%H:%M:%SZ'))
                    - _time.mktime(_time.strptime(
                        entry['at'], '%Y-%m-%dT%H:%M:%SZ')))
            except Exception:                              # noqa: BLE001
                seconds = 0
            rounds.append({
                'at': exit_['at'], 'token': token,
                'bought': entry['price'], 'sold': exit_['price'],
                'size': size, 'gain_usd': round(gain, 4),
                'minutes': round(seconds / 60, 1),
            })
        else:
            waiting[(token, side)].append(fill)
    rounds.reverse()
    return rounds


def _drift_summary(rows):
    """
    Неблагоприятный отбор: куда шла цена после наших исполнений.

    ЧИСЛО, РЕШАЮЩЕЕ СУДЬБУ ЗАТЕИ. Спред известен заранее и выглядит щедро;
    сколько из него отбирают обратно — видно только так. Отрицательная сумма
    означает, что нас исполняют перед движением против нас.

    ВАЖНАЯ ОГОВОРКА, которую панель обязана показывать: по ЗАВЕРШЁННОМУ кругу
    снос сокращается сам собой — на покупке он одного знака, на продаже
    другого. Бьёт он по позициям, которые не закрылись, и судить надо по ним.
    """
    if not rows:
        return {'count': 0}
    total = sum(r.get('gain_usd') or 0 for r in rows)
    per = [r.get('gain_per_contract') or 0 for r in rows]
    return {
        'count': len(rows),
        'total_usd': round(total, 4),
        'avg_per_contract': round(sum(per) / len(per), 5),
        'against_us': sum(1 for x in per if x < 0),
        'for_us': sum(1 for x in per if x > 0),
        'last': rows[-5:],
    }


def timing_summary(rows):
    """
    Обещание модели против дела: во сколько раз она оптимистична.

    ЗАЧЕМ ЭТО ЧИСЛО ВООБЩЕ ЕСТЬ. Ожидание считается в предположении, что
    стороны независимы — бид ждёт своего потока, аск своего. На деле они
    связаны и связаны ПРОТИВ нас: цена ушла вниз, покупку исполнили, продажу
    нет. Поправка на это не выводится из книги; её можно только измерить.

    Пока исполнений мало, поправка не применяется: пять наблюдений — это шум,
    а не поправка. Порог намеренно назван в коде, а не подобран на глаз.
    """
    good = [r for r in rows or [] if r.get('ratio')]
    if not good:
        return {'count': 0, 'factor': 1.0, 'enough': False}
    ratios = sorted(float(r['ratio']) for r in good)
    middle = ratios[len(ratios) // 2]
    promised = sorted(float(r['promised_seconds']) for r in good)
    waited = sorted(float(r['waited_seconds']) for r in good)
    enough = len(good) >= MIN_TIMING_SAMPLES
    return {
        'count': len(good),
        'promised_min': round(promised[len(promised) // 2] / 60, 1),
        'waited_min': round(waited[len(waited) // 2] / 60, 1),
        'factor': round(middle, 2) if enough else 1.0,
        'measured': round(middle, 2),
        'enough': enough,
        'need': max(0, MIN_TIMING_SAMPLES - len(good)),
    }


# Сколько исполнений нужно, чтобы поправка перестала быть шумом.
MIN_TIMING_SAMPLES = 20


def wait_factor():
    """
    Во сколько раз делить расчётный доход. 1.0 — замеров ещё мало.

    Читается из журнала, а не из настройки: поправка должна приходить из дела,
    а не из чьего-то мнения о деле.
    """
    import json
    import os

    from . import engine

    rows = []
    try:
        with open(engine.TIMING, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except (OSError, ValueError):
        return 1.0
    from . import params

    got = float(timing_summary(rows[-200:]).get('factor') or 1.0)
    # ОГРАНИЧИТЕЛЬ. Несколько мгновенных исполнений дают медиану 0.01 — «модель
    # врёт в сто раз», — и без предела это открыло бы список рынкам, где круг
    # по расчёту занимает недели.
    return max(params.MM_WAIT_FACTOR_MIN, min(params.MM_WAIT_FACTOR_MAX, got))


def _shadow_summary(rows):
    """
    Модель против биржи: сходится ли бумажный расчёт с настоящими сделками.

    Пишется только в живом режиме. Расхождение — самое ценное, что даст живая
    торговля: оно скажет, насколько можно верить бумаге, когда придёт время
    увеличивать размер.
    """
    if not rows:
        return {'count': 0}
    said_filled = 0
    for row in rows:
        for side in (row.get('sides') or {}).values():
            if side.get('model_filled'):
                said_filled += 1
    return {'count': len(rows), 'model_said_filled': said_filled,
            'last_at': rows[-1].get('at')}


def snapshot(limit_markets=20, limit_fills=30):
    """
    Состояние маркет-мейкера для панели. Ключа здесь нет и быть не может.

    Читается ИЗ ФАЙЛОВ, а не из работающего процесса: маркет-мейкер живёт
    отдельно, и панель не должна ни ждать его, ни падать вместе с ним. Данных
    нет — значит он не запускался, и так и сообщаем.
    """
    import json
    import os

    from . import engine, executor, mm, service, store, wallet

    def _tail(path, count):
        if not os.path.exists(path):
            return []
        rows = []
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-count:]

    state = {}
    if os.path.exists(engine.STATE):
        try:
            with open(engine.STATE, encoding='utf-8') as fh:
                state = json.load(fh)
        except Exception:                                  # noqa: BLE001
            state = {}

    books = state.get('books') or {}
    known = mm.known_markets()
    positions = []
    for token, slot in books.items():
        if not slot.get('position'):
            continue
        positions.append({
            'token': token,
            'question': (known.get(str(token)) or {}).get('question') or '—',
            'position': slot['position'],
            'avg_cost': round(slot.get('avg_cost') or 0, 4),
            'realized': round(slot.get('realized') or 0, 2),
            'trades': slot.get('trades') or 0,
        })
    positions.sort(key=lambda r: -abs(r['position']))

    equity = _tail(engine.EQUITY, 200)
    fills = _tail(engine.FILLS, limit_fills)
    orders = _tail(executor.ORDERS_LOG, 40)

    quoting = sum(1 for slot in books.values()
                  if (slot.get('orders') or {}).get('bid')
                  or (slot.get('orders') or {}).get('ask'))

    # ЧТО БОТ ДЕЛАЕТ ПРЯМО СЕЙЧАС. Прежде панель показывала только позиции, то
    # есть рынки, где нас УЖЕ исполнили. Но большую часть времени бот именно
    # СТОИТ — и на вопрос «чем он занят» ответить было нечем: девять рынков,
    # ноль позиций, и ни строчки о том, где мы и почём.
    plan_on_disk = {}
    if os.path.exists(mm.PLAN_FILE):
        try:
            with open(mm.PLAN_FILE, encoding='utf-8') as fh:
                plan_on_disk = json.load(fh)
        except Exception:                                  # noqa: BLE001
            plan_on_disk = {}
    live_quotes = _standing_quotes(books, plan_on_disk.get('markets') or [],
                                   catalogue=mm.known_markets())

    # ЗАВЕРШЁННЫЕ КРУГИ — главная мерка работы мейкера. Отдельные исполнения
    # ничего не говорят: купить может каждый, а заработок появляется только
    # когда позиция закрыта. Круги считаются по журналу, а не по состоянию:
    # состояние помнит текущее, журнал — всё.
    rounds = _round_trips(_tail(engine.FILLS, 400))

    drift = _drift_summary(_tail(engine.DRIFT, 200))
    timing = timing_summary(_tail(engine.TIMING, 200))
    shadow = _shadow_summary(_tail(mm.SHADOW, 200))

    # СОСТОЯНИЕ БЕРЁТСЯ У ПОТОКА, А НЕ ПО НАЛИЧИЮ ФАЙЛОВ. Файлы остаются от
    # прошлых запусков, и по ним «работает» показывалось бы и после остановки.
    thread = service.status()
    return {
        'running': bool(thread.get('alive')),
        'has_history': bool(equity),
        'service': thread,
        'started': state.get('started'),
        'wallet': wallet.status(),
        # Состояние подключения для панели: адрес, остаток, бюджет. Ключа
        # здесь нет и быть не может — по адресу всё видно и ничего нельзя
        # подписать.
        'connect': _connect_state(),
        'kill_switch': executor.kill_switch_on(),
        # ФАКТ ОТ БИРЖИ РЯДОМ С РАСЧЁТОМ БОТА. Без этого панель отвечала
        # моделью на вопрос о деньгах, и расхождение с Polymarket выглядело
        # как ложь одной из сторон.
        'exchange': _exchange_safely(),
        'equity': equity[-1] if equity else None,
        'equity_series': [{'at': e.get('at'), 'equity': e.get('equity'),
                           'realized': e.get('realized'),
                           'inventory': e.get('inventory')}
                          for e in equity[-120:]],
        'positions': positions[:limit_markets],
        'positions_total': len(positions),
        'fills': list(reversed(fills)),
        'fills_total': sum(1 for _ in fills),
        'orders_log': list(reversed(orders))[:20],
        'quoting_markets': quoting,
        # Что бот делает прямо сейчас, чего добился и врёт ли модель.
        'standing': live_quotes,
        'rounds': rounds[:30],
        'rounds_total': len(rounds),
        'rounds_gain_usd': round(sum(r['gain_usd'] for r in rounds), 4),
        'drift': drift,
        'shadow': shadow,
        # Обещание модели против дела. Пока замеров мало, поправка не
        # применяется — но видно, сколько их и куда они клонят.
        'timing': timing,
        # Статистика для улучшения стратегии: что вышло на самом деле, рядом
        # с тем, что обещал отбор. Читается с диска и переживает перезапуск.
        'stats': _stats_safely(),
        # Раскладка бюджета: во что вложено и на что рассчитываем. Без неё
        # человек видит «работает» и не знает, чего ждать, а ждать при сотне
        # долларов приходится единиц долларов в месяц — и лучше знать это
        # заранее, чем обнаружить через месяц.
        'plan': {k: v for k, v in (mm.LAST_PLAN or {}).items() if k != 'markets'},
    }
