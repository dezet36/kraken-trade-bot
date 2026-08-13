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

    # ── Состояние ────────────────────────────────────────────────────────────

    def _blank(self):
        return {'started': _stamp(), 'cash': self.bankroll,
                'books': {}, 'version': 1}

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
            done.append({'at': _stamp(), 'token': token,
                         'condition': condition_id, 'side': side,
                         'price': order['price'], 'size': order['size'],
                         'queue_ahead': order['queue'],
                         'seconds_to_fill': verdict['ts'] - order['ts'],
                         'position_after': slot['position'],
                         'realized_after': round(slot['realized'], 4)})
            slot['orders'][side] = None
        return done

    def place(self, token, quote, top, book):
        """
        Выставляет (переставляет) двусторонние заявки.

        Очередь перед нами запоминается В МОМЕНТ ВЫСТАВЛЕНИЯ и потом не
        пересчитывается: она и есть то, что нам придётся пропустить. Обновляя
        её каждый цикл, мы бы вечно «подходили к началу» и рисовали себе
        исполнения.
        """
        slot = self._slot(token)
        orders = slot.setdefault('orders', {})
        for side in ('bid', 'ask'):
            if quote.get('only') and quote['only'] != side:
                orders[side] = None
                continue
            price = quote[side]
            current = orders.get(side)
            if current and abs(current['price'] - price) < 1e-9:
                continue                        # цена не изменилась — не трогаем
            orders[side] = {
                'price': price, 'size': quote['size'], 'ts': _now(),
                'queue': book_mod.depth_ahead(book, side, price),
            }
        return orders

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
