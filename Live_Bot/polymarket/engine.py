"""
Бумажный маркет-мейкер: заявки, исполнения, запас и результат.

ЧТО ЭТО ТАКОЕ. Полный цикл котирования без единого обращения к торговому API:
заявки существуют только у нас, исполнения определяются по ленте сделок биржи.
Это не «упрощённая версия боевого» — это единственный способ проверить
маркет-мейкинг вообще, потому что на исторических ценах он не проверяется:
там нет ни очереди заявок, ни того, кто кого снял.

УЧЁТ ВЕДЁТСЯ ПО СРЕДНЕЙ ЦЕНЕ, И ЭТО НЕ ПРОИЗВОЛ. Купили сто по 0.20 и сто по
0.30, продали сто по 0.28 — прибыль зависит от того, что мы считаем проданным.
Средняя цена (0.25) даёт +0.03 на контракт и не позволяет выбирать выгодную
трактовку задним числом. Метод «первым пришёл — первым ушёл» дал бы +0.08 на тех
же данных, то есть красивее, а на длинной дистанции — ту же сумму. Разница
только в том, когда прибыль признаётся; выбираем ту, что не льстит.

ЗАПАС ОЦЕНИВАЕТСЯ ПО СЕРЕДИНЕ РЫНКА, А НЕ ПО ЦЕНЕ ПОКУПКИ. Оценка по покупке
скрывала бы ровно ту болезнь, ради лечения которой всё затевалось: разобранный
кошелёк держит 2 236 позиций с переоценкой -$8 564 и по «цене покупки» выглядел
бы прибыльным.

СОСТОЯНИЕ СОХРАНЯЕТСЯ НА ДИСК каждый цикл. Перезапуск не должен ни терять
позиции, ни начинать учёт заново: и то и другое превратило бы недельный замер
в набор несвязанных получасовых.
"""

import json
import os
import time

from . import book as book_mod
from . import client, params, store, strategy

STATE = os.path.join(store.DIR, 'mm_state.json')
FILLS = os.path.join(store.DIR, 'mm_fills.jsonl')
EQUITY = os.path.join(store.DIR, 'mm_equity.jsonl')
# Снос цены после наших исполнений — то единственное число, которое решает,
# работает ли затея. Спред известен заранее; неблагоприятный отбор — нет.
DRIFT = os.path.join(store.DIR, 'mm_drift.jsonl')
# ОБЕЩАНИЕ МОДЕЛИ ПРОТИВ ДЕЛА. Расчёт ожидания предполагает, что стороны
# независимы: бид и аск ждут каждый своего потока. На деле они связаны и
# связаны ПРОТИВ нас — цена ушла вниз, покупку исполнили, продажу нет. Насколько
# именно расчёт врёт, узнать можно только сверкой обещанного с вышедшим, и
# другого способа нет: на исторических ценах очереди не существует.
TIMING = os.path.join(store.DIR, 'mm_timing.jsonl')


def _now():
    return int(time.time())


def _stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class PaperMaker:
    """Бумажный маркет-мейкер по многим рынкам сразу."""

    def __init__(self, bankroll=None, state_path=None):
        self.state_path = state_path or STATE
        self.bankroll = float(bankroll or params.bankroll_for('MM'))
        self.state = self._load()
        self.budget_note = self._sync_budget()

    def _sync_budget(self):
        """
        Подтягивает изменённый бюджет к сохранённому состоянию.

        БЕЗ ЭТОГО НАСТРОЙКА ЛИШЬ КАЗАЛАСЬ БЫ РАБОТАЮЩЕЙ. Состояние хранит
        деньги на диске, и при запуске с новым бюджетом бот печатал бы «капитал
        $250», а тратил бы прежние $450 — потому что цифра берётся из файла, а
        не из настройки. Поймано на первом же запуске с изменённой суммой.

        ДЕНЬГИ ПЕРЕСТАВЛЯЮТСЯ ТОЛЬКО НА ЧИСТОМ СОСТОЯНИИ. Если есть открытые
        позиции, менять размер счёта под ними нельзя: доходность считается от
        начального капитала, и подмена знаменателя на ходу превратила бы замер
        в бессмыслицу. Тогда — предупреждение, а не тихая правка.
        """
        saved = self.state.get('bankroll')
        if saved is not None and abs(float(saved) - self.bankroll) < 1e-9:
            return ''

        # ЧИСТОЕ СОСТОЯНИЕ ОПРЕДЕЛЯЕТСЯ ПО ДЕЛУ, А НЕ ПО ЗАПИСИ О БЮДЖЕТЕ.
        # Первая версия смотрела на сохранённый бюджет и, не найдя его (а в
        # состояниях, созданных прежним кодом, его и нет), переписывала деньги
        # ПОВЕРХ открытой позиции. Ровно то, чего эта функция обязана не делать.
        # Поймано запуском на живом состоянии, а не рассуждением.
        books = self.state.get('books', {}) or {}
        busy = [t for t, s in books.items() if s.get('position')]
        traded = [t for t, s in books.items() if s.get('trades')]
        if busy or traded:
            was = f'${float(saved):,.2f}' if saved is not None else 'прежняя сумма'
            return (f'бюджет изменён ({was} → ${self.bankroll:,.2f}), но '
                    f'состояние уже в работе: позиций {len(busy)}, '
                    f'торговавших рынков {len(traded)}. Новая сумма вступит в '
                    f'силу после сброса состояния.')

        self.state['bankroll'] = self.bankroll
        self.state['cash'] = self.bankroll
        return f'бюджет применён к чистому состоянию: ${self.bankroll:,.2f}'

    # ── Состояние ────────────────────────────────────────────────────────────

    def _blank(self):
        return {'started': _stamp(), 'cash': self.bankroll,
                # Бюджет запоминается вместе с деньгами: иначе при следующем
                # запуске нельзя отличить «настройку изменили» от «столько и
                # было», а значит нельзя решить, применять её или нет.
                'bankroll': self.bankroll, 'books': {}, 'version': 1}

    def _load(self):
        if not os.path.exists(self.state_path):
            return self._blank()
        try:
            with open(self.state_path, encoding='utf-8') as fh:
                data = json.load(fh)
            if isinstance(data, dict) and 'books' in data:
                return data
        except Exception:                                   # noqa: BLE001
            pass
        return self._blank()

    def save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self.state, fh, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def _slot(self, token):
        """Учётная запись по одному токену."""
        return self.state['books'].setdefault(str(token), {
            'position': 0.0, 'avg_cost': 0.0, 'realized': 0.0,
            'orders': {}, 'trades': 0, 'opened_ts': None,
        })

    # ── Торговля ─────────────────────────────────────────────────────────────

    def _apply_fill(self, slot, side, price, size):
        """
        Исполнение: меняет позицию, среднюю цену и зафиксированный результат.

        Прибыль признаётся только при СОКРАЩЕНИИ позиции. Наращивание меняет
        среднюю цену и ничего не фиксирует — иначе покупка сама по себе
        выглядела бы прибыльной или убыточной, чем она не является.
        """
        position = slot['position']
        signed = size if side == 'bid' else -size
        # Комиссия мейкера равна нулю — в этом весь смысл стратегии. Тейкер на
        # цене 0.05 отдал бы 4.75% ставки; здесь не платится ничего.
        if position == 0 or (position > 0) == (signed > 0):
            total = abs(position) + size
            slot['avg_cost'] = ((slot['avg_cost'] * abs(position)
                                 + price * size) / total) if total else price
            if position == 0:
                slot['opened_ts'] = _now()
        else:
            closing = min(size, abs(position))
            direction = 1 if position > 0 else -1
            slot['realized'] += direction * (price - slot['avg_cost']) * closing
            if size > abs(position):
                # Позиция перевернулась: остаток становится новой по цене сделки.
                slot['avg_cost'] = price
                slot['opened_ts'] = _now()
        slot['position'] = position + signed
        slot['trades'] += 1
        if abs(slot['position']) < 1e-9:
            slot['position'] = 0.0
            slot['avg_cost'] = 0.0
            slot['opened_ts'] = None
        return slot

    def process_fills(self, token, condition_id, trades):
        """
        Проверяет свои заявки по ленте и исполняет те, до которых дошло.

        Заявка снимается после исполнения: частичные исполнения не моделируются
        сознательно. Модель очереди и так пессимистична, а частичное исполнение
        добавило бы правдоподобия там, где проверить его нечем.
        """
        slot = self._slot(token)
        done = []
        for side in ('bid', 'ask'):
            order = (slot['orders'] or {}).get(side)
            if not order:
                continue
            fresh = [t for t in (trades or []) if t['ts'] >= order['ts']]
            verdict = book_mod.would_fill(side, order['price'],
                                          order['queue'], fresh, token_id=token)
            if not verdict or not verdict['filled']:
                continue
            self._apply_fill(slot, side, order['price'], order['size'])
            cash = -order['price'] * order['size'] if side == 'bid' \
                else order['price'] * order['size']
            self.state['cash'] += cash
            self._note_timing(token, side, order, at=verdict['ts'])
            done.append({'at': _stamp(),
                         # ПОМЕТКА ПРОГОНА. Журнал исполнений не чистится при
                         # перезапуске, и записи прежних прогонов дважды едва
                         # не были выданы за новые: состояние сбрасывается, а
                         # журнал остаётся. Без пометки отличить их можно было
                         # только сравнением времени вручную.
                         'run': self.state.get('started'), 'token': token,
                         'condition': condition_id, 'side': side,
                         'price': order['price'], 'size': order['size'],
                         'queue_ahead': order['queue'],
                         'seconds_to_fill': verdict['ts'] - order['ts'],
                         'position_after': slot['position'],
                         'realized_after': round(slot['realized'], 4)})
            slot['orders'][side] = None
        return done

    def predict_fills(self, token, trades):
        """
        Что сказала бы модель очереди — БЕЗ изменения состояния.

        ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ. Заголовок модуля обещает, что бумажный расчёт
        не выключается в живом режиме и служит меркой для реальности. Обещание
        было ложным: в живом режиме код просто пропускал ленту, и сравнивать
        оказывалось нечего. Первый живой прогон не сказал бы о точности модели
        ровно ничего.

        Здесь модель отвечает на свой вопрос, ничего не трогая: позиции и
        деньги ведёт биржа, а модель идёт рядом и оставляет своё мнение. Их
        расхождение и есть самое ценное, что даст живой режим — оно скажет,
        насколько можно верить бумаге, когда придёт время увеличивать размер.
        """
        slot = self._slot(token)
        out = {}
        for side in ('bid', 'ask'):
            order = (slot.get('orders') or {}).get(side)
            if not order:
                continue
            fresh = [t for t in (trades or []) if t['ts'] >= order['ts']]
            verdict = book_mod.would_fill(side, order['price'], order['queue'],
                                          fresh, token_id=token)
            out[side] = {'price': order['price'], 'size': order['size'],
                         'queue': order['queue'],
                         'model_filled': bool(verdict and verdict['filled'])}
        return out

    def watch_drift(self, fills, marks):
        """
        Ставит исполнение на учёт: куда ушла цена ПОСЛЕ того, как нас исполнили.

        ЭТО ЕДИНСТВЕННОЕ ЧИСЛО, КОТОРОЕ РЕШАЕТ, РАБОТАЕТ ЛИ ЗАТЕЯ. Спред мы
        знаем заранее и он выглядит щедро; чего мы не знаем — сколько из него
        отбирает неблагоприятный отбор. Нас исполняют не в случайный момент, а
        ровно тогда, когда встречной стороне это выгодно, то есть когда цена
        уже пошла против нас. Мейкер зарабатывает разницу между спредом и этим
        сносом, и если снос больше — не помогут ни частота, ни число рынков.

        Меряется просто: запоминаем середину рынка в момент исполнения и
        сравниваем её же через полчаса. Купили и середина упала — снос против
        нас. Продали и середина выросла — то же самое.
        """
        for fill in fills or []:
            mid = marks.get(str(fill['token']))
            if mid is None:
                continue
            self.state.setdefault('drift_pending', []).append({
                'token': str(fill['token']), 'side': fill['side'],
                'price': fill['price'], 'size': fill['size'],
                'mid_at_fill': mid, 'ts': _now()})

    def measure_drift(self, marks, minutes=None):
        """
        Закрывает созревшие замеры сноса и возвращает их.

        Незрелые не трогаются, а рынки без цены ЖДУТ, а не выбрасываются:
        выбросив их, мы бы выбрасывали ровно те случаи, где книга опустела
        после нашего исполнения, — то есть худшие из возможных.
        """
        wait = float(minutes if minutes is not None
                     else params.MM_DRIFT_MINUTES) * 60
        ripe, pending = [], []
        for item in self.state.get('drift_pending', []):
            mid = marks.get(item['token'])
            if _now() - item['ts'] < wait or mid is None:
                pending.append(item)
                continue
            moved = mid - item['mid_at_fill']
            # Знак приводится к нашей выгоде: после покупки рост — в плюс,
            # после продажи рост — в минус.
            gain = moved if item['side'] == 'bid' else -moved
            ripe.append({'at': _stamp(), **item, 'mid_now': mid,
                         'moved': round(moved, 6),
                         'gain_per_contract': round(gain, 6),
                         'gain_usd': round(gain * item['size'], 4),
                         'minutes': round((_now() - item['ts']) / 60, 1)})
        self.state['drift_pending'] = pending
        return ripe

    def place(self, token, quote, top, book, market_tick=None):
        """
        Выставляет (переставляет) двусторонние заявки.

        Очередь перед нами запоминается В МОМЕНТ ВЫСТАВЛЕНИЯ и потом не
        пересчитывается: она и есть то, что нам придётся пропустить. Обновляя
        её каждый цикл, мы бы вечно «подходили к началу» и рисовали себе
        исполнения.

        Возвращает (заявки, список биржевых номеров на снятие).
        """
        market_tick = market_tick or quote.get('tick') or 0.001
        slot = self._slot(token)
        orders = slot.setdefault('orders', {})
        # Заявки, которые заменяются или снимаются, возвращаются наружу СО
        # СВОИМ биржевым номером. Без этого старая живая заявка осталась бы
        # лежать в стакане по устаревшей цене: мы бы её не видели, а исполнить
        # нас по ней могли — и именно тогда, когда это выгодно встречной
        # стороне. За смену часов таких заявок накопились бы сотни.
        replaced = []
        for side in ('bid', 'ask'):
            current = orders.get(side)
            if quote.get('only') and quote['only'] != side:
                if current and current.get('live_id'):
                    replaced.append(current['live_id'])
                orders[side] = None
                continue
            price = quote[side]
            # ЗАЯВКА НЕ ПЕРЕСТАВЛЯЕТСЯ ИЗ-ЗА КАЖДОГО ДРОЖАНИЯ ЦЕНЫ.
            #
            # Здесь стояло сравнение на точное равенство: сдвинулась цена на
            # один тик — снимаем и ставим заново. А новая заявка встаёт в КОНЕЦ
            # новой очереди, то есть каждая перестановка обнуляет всё
            # накопленное ожидание.
            #
            # Замерено за сутки: 125 выставленных заявок на 29 котировок, то
            # есть 4.3 перестановки на каждую, а у отдельных по шестнадцать.
            # При обещанном круге в 46 минут мы шестнадцать раз возвращались в
            # хвост очереди и не доходили никуда — отсюда и доля исполнения в
            # 13.8%.
            #
            # Допуск в два тика ограничивает и обратный риск: если рынок уйдёт
            # против нас, мы переставимся, сдвинувшись не больше чем на два
            # тика — величину того же порядка, что и захватываемый спред.
            room = params.MM_REQUOTE_TICKS * float(market_tick or 0.001)
            # ЗАЯВКА БЕЗ КРАЯ СНИМАЕТСЯ НЕМЕДЛЕННО, СКОЛЬКО БЫ ТИКОВ НИ БЫЛО.
            #
            # ЭТО САМАЯ ДОРОГАЯ НАХОДКА ЗА ВЕСЬ РАЗБОР. Главная мерка мейкера —
            # сколько мы берём относительно СЕРЕДИНЫ в момент исполнения.
            # Замер по 21 исполнению:
            #
            #     медиана края: +0.00000
            #     взяли спред 10 раз, отдали или в ноль — 11
            #     bid 0.4350 при середине 0.4155 — купили ВЫШЕ середины
            #     ask 0.1930 при середине 0.2035 — продали НИЖЕ середины
            #
            # Нулевой край означает нулевое ожидание до всяких издержек: мы не
            # делали маркет-мейкинг, мы торговали по справедливой цене.
            #
            # Причина — допуск в два тика, поставленный ради места в очереди.
            # Рынок сдвигался на тик, заявка оставалась, и её подбирали ровно
            # тогда, когда она переставала быть выгодной. Место в очереди
            # дорого, но не дороже смысла всей затеи.
            #
            # Порог мягкий: край должен быть хотя бы в полтика, иначе держать
            # заявку незачем.
            keep = current is not None and abs(current['price'] - price) < max(room, 1e-9)
            if keep and top and top.get('mid') is not None:
                edge = ((top['mid'] - current['price']) if side == 'bid'
                        else (current['price'] - top['mid']))
                if edge < float(market_tick or 0.001) / 2:
                    keep = False
            if keep:
                continue                        # цена почти та же — не трогаем
            if current and current.get('live_id'):
                replaced.append(current['live_id'])
            orders[side] = {
                'price': price, 'size': quote['size'], 'ts': _now(),
                'queue': book_mod.depth_ahead(book, side, price),
                # Обещание модели едет вместе с заявкой: сравнить его с делом
                # можно только в момент исполнения, а к тому времени план
                # отбора успевает смениться.
                'expected_seconds': quote.get('expected_seconds'),
            }
        return orders, replaced

    # ── Отчётность ───────────────────────────────────────────────────────────

    def mark_to_market(self, marks):
        """
        Капитал: деньги плюс запас, оценённый по СЕРЕДИНЕ рынка.

        marks — {токен: середина}. Токены без цены оцениваются по своей средней
        цене: это единственная доступная оценка, и она не льстит.
        """
        inventory = 0.0
        for token, slot in self.state['books'].items():
            if not slot['position']:
                continue
            mark = marks.get(token)
            if mark is None:
                mark = slot['avg_cost']
            inventory += slot['position'] * mark
        realized = sum(s['realized'] for s in self.state['books'].values())
        return {'cash': self.state['cash'], 'inventory': inventory,
                'equity': self.state['cash'] + inventory,
                'realized': realized,
                'pnl': self.state['cash'] + inventory - self.bankroll}

    def exposure(self, marks):
        """Сколько денег стоит в запасе — по модулю, обе стороны считаются."""
        total = 0.0
        for token, slot in self.state['books'].items():
            if not slot['position']:
                continue
            mark = marks.get(token) or slot['avg_cost']
            total += abs(slot['position'] * mark)
        return total

    def stale_positions(self, hours=None):
        """Позиции, висящие дольше срока: доход здесь спред, а не исход."""
        limit = float(hours or params.MM_MAX_HOLD_HOURS) * 3600
        now = _now()
        return [token for token, slot in self.state['books'].items()
                if slot['position'] and slot.get('opened_ts')
                and now - slot['opened_ts'] > limit]

    def apply_exchange_trades(self, trades, seen_ids=None):
        """
        Проводит НАСТОЯЩИЕ сделки с биржи. Источник правды в живом режиме.

        В бумаге исполнение оценивается по ленте и модели очереди — иначе
        никак. Как только заявки уходят на биржу, такая оценка становится
        вредной: она отвечает «исполнилось бы», а биржа знает «исполнилось».
        Разойдутся они обязательно, потому что очередь оценивается
        приблизительно, и учёт поехал бы вслед за оценкой.

        Повторы отсекаются по идентификатору сделки: тот же ответ придёт и в
        следующем цикле, а провести его дважды значит удвоить позицию.
        """
        seen = set(seen_ids or self.state.setdefault('seen_trades', []))
        done = []
        for trade in trades or []:
            key = str(trade.get('id') or '')
            if not key or key in seen:
                continue
            seen.add(key)
            slot = self._slot(trade['token'])
            self._apply_fill(slot, trade['side'], trade['price'], trade['size'])
            cash = -trade['price'] * trade['size'] if trade['side'] == 'bid' \
                else trade['price'] * trade['size']
            self.state['cash'] += cash
            # Заявка, по которой прошло исполнение, у биржи уже закрыта.
            # ЗАОДНО СВЕРЯЕМ ОБЕЩАНИЕ С ДЕЛОМ. Другого способа узнать, врёт ли
            # расчёт ожидания, не существует: на исторических ценах очереди нет
            # вовсе, а бумажная модель отвечает на свой же вопрос.
            for side in ('bid', 'ask'):
                order = (slot.get('orders') or {}).get(side)
                if order and order.get('live_id') == trade.get('order_id'):
                    self._note_timing(trade['token'], side, order)
                    slot['orders'][side] = None
            done.append({'at': _stamp(), 'source': 'exchange',
                         'token': trade['token'], 'side': trade['side'],
                         'price': trade['price'], 'size': trade['size'],
                         'trade_id': key,
                         'position_after': slot['position'],
                         'realized_after': round(slot['realized'], 4)})
        # Список виденных обрезается: он нужен только чтобы не провести сделку
        # дважды, а храниться вечно ему незачем.
        self.state['seen_trades'] = list(seen)[-5000:]
        return done

    def _note_timing(self, token, side, order, at=None):
        """
        Кладёт в журнал обещанное время и вышедшее.

        ЧИСЛО, БЕЗ КОТОРОГО ВЕСЬ РАСЧЁТ ОСТАЁТСЯ АРИФМЕТИКОЙ. Ожидание
        считается в предположении, что стороны НЕЗАВИСИМЫ: бид ждёт своего
        потока, аск своего. На деле они связаны, и связаны против нас — цена
        ушла вниз, покупку исполнили, продажу нет. Насколько именно расчёт
        оптимистичен, показывает только сверка обещанного с вышедшим, и делать
        её надо на каждом исполнении: их пока единицы, и терять нельзя ни одно.
        """
        promised = order.get('expected_seconds')
        if not promised:
            return
        waited = max(0, int(at or _now()) - int(order.get('ts') or _now()))
        try:
            store._append(TIMING, {
                'at': _stamp(), 'token': str(token), 'side': side,
                'promised_seconds': round(float(promised)),
                'waited_seconds': waited,
                'ratio': round(waited / float(promised), 3),
            })
        except Exception:                                   # noqa: BLE001
            pass

    def live_order_ids(self):
        """Биржевые номера всех наших заявок — для сверки с биржей."""
        out = {}
        for token, slot in self.state['books'].items():
            for side, order in (slot.get('orders') or {}).items():
                if order and order.get('live_id'):
                    out[str(order['live_id'])] = (token, side, order['price'])
        return out

    def forget_orders(self, order_ids):
        """
        Забывает заявки, которых на бирже уже нет.

        Вызывается по итогам сверки. Держать у себя заявку, которой биржа не
        знает, опаснее, чем не держать: мы считаем, что котируем эту сторону, и
        не выставляем её заново — то есть перестаём сокращать запас, полагая,
        что сокращаем.
        """
        gone = {str(i) for i in (order_ids or [])}
        dropped = 0
        for slot in self.state['books'].values():
            for side, order in list((slot.get('orders') or {}).items()):
                if order and str(order.get('live_id')) in gone:
                    slot['orders'][side] = None
                    dropped += 1
        return dropped
