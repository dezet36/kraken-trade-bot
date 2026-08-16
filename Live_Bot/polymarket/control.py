"""
Постоянный контроль позиций: где рынок, где мы, что будет дальше.

ЗАЧЕМ ОТДЕЛЬНЫЙ ВЗГЛЯД, ЕСЛИ ЕСТЬ СПИСОК ПОЗИЦИЙ И СПИСОК ЗАЯВОК. Потому что
по ним нельзя ответить на единственный важный вопрос: ведётся эта позиция или
висит мёртвым грузом. Список позиций говорит «держим пять контрактов», список
заявок — «стоит продажа по 0.619». Ни один не говорит, что рынок ушёл на 0.474 и
эта продажа не исполнится никогда.

Замерено на живом счёте ровно в таком виде:

    «Max Martin»  держим 5 по 0.637, продаём по 0.619, рынок 0.474
                  до середины 0.145 — четырнадцать центов мимо
    «Democrats win Virginia»  впереди 4 827 контрактов, тишина 14 часов
    «Democratic Party IA-03»  встречного потока нет вовсе, тишина 33 часа

Каждая из них выглядела как работающая заявка. Ни одна не могла исполниться.

ЧТО СЧИТАЕТСЯ ЗДЕСЬ И ЧЕГО ЗДЕСЬ НЕТ. Это наблюдение, а не решение: модуль
ничего не переставляет и не отменяет. Он отвечает на вопрос «что происходит», и
отвечает по числам биржи — цена, очередь, срок. Решения принимает стратегия, и
смешивать их с наблюдением нельзя: тогда отчёт начнёт оправдывать сам себя.
"""

import time

from . import book as book_mod
from . import params

# Приговоры, от худшего к лучшему. Порядок нужен панели: сортировать надо так,
# чтобы застрявшее было сверху, а не терялось среди работающего.
VERDICTS = ('нет книги', 'без заявки', 'вне рынка', 'за очередью',
            'ждём очереди', 'первые в очереди')


def _queue_ahead(live, side, price, ours):
    """
    Сколько чужого объёма исполнится раньше нас.

    Считается всё, что стоит по более выгодной для встречной стороны цене, плюс
    чужая часть НАШЕГО уровня: своя заявка себя не задерживает.
    """
    levels = (live.get('bids') if side == 'bid' else live.get('asks')) or []
    better = sum(size for level, size in levels
                 if (level > price + 1e-9 if side == 'bid'
                     else level < price - 1e-9))
    same = sum(size for level, size in levels if abs(level - price) < 1e-9)
    return better + max(same - float(ours or 0), 0.0)


def _hours_left(opened_ts, limit_hours, now):
    """Сколько часов осталось до срока. Отрицательное — срок уже прошёл."""
    if not opened_ts:
        return None
    return round(limit_hours - (now - float(opened_ts)) / 3600, 2)


def review(maker, books, catalogue=None, orders=None, now=None):
    """
    Состояние каждой позиции одной строкой: рынок, наша цена, приговор, срок.

    `books` — стаканы по токенам, как их отдаёт book.fetch_many.
    `orders` — заявки биржи {токен: [заявки]}; без них смотрим свой учёт.

    ЗАЯВКА ИЩЕТСЯ И ПО ВСТРЕЧНОМУ ТОКЕНУ. Продажа «ДА» уходит на биржу покупкой
    «НЕТ», и по основному токену её не найти: снаружи это выглядело бы как
    «позиция без заявки» на каждом втором рынке.
    """
    now = float(now if now is not None else time.time())
    catalogue = catalogue or {}
    orders = orders or {}
    rows = []
    for token, slot in (maker.state.get('books') or {}).items():
        position = float(slot.get('position') or 0)
        if not position:
            continue
        card = catalogue.get(str(token)) or {}
        tick = float(card.get('tick') or 0.01)
        live = books.get(str(token))
        top = book_mod.top(live) if live else None
        # Продаём, если держим; покупаем, если держим встречный.
        side = 'ask' if position > 0 else 'bid'

        row = {
            'token': str(token),
            'question': card.get('question'),
            'position': round(position, 2),
            'avg_cost': round(float(slot.get('avg_cost') or 0), 4),
            'side': side,
            'held_hours': (round((now - float(slot['opened_ts'])) / 3600, 2)
                           if slot.get('opened_ts') else None),
            'until_best_price': _hours_left(slot.get('opened_ts'),
                                            params.MM_MAX_HOLD_HOURS, now),
            'until_market_exit': _hours_left(
                slot.get('opened_ts'),
                params.MM_MAX_HOLD_HOURS * params.MM_DESPERATE_AFTER, now),
        }

        if not top or top.get('mid') is None:
            row.update({'verdict': 'нет книги',
                        'why': 'стакан не пришёл — цену взять неоткуда'})
            rows.append(row)
            continue

        row['mid'] = top['mid']
        row['market'] = {'bid': top['bid'], 'ask': top['ask']}
        row['value'] = round(abs(position) * (top['mid'] if position > 0
                                              else 1 - top['mid']), 2)
        row['unrealised'] = round(
            (top['mid'] - row['avg_cost']) * position, 4)

        ours = None
        for candidate in (str(token), str(card.get('token_no') or '')):
            for order in orders.get(candidate) or []:
                price = float(order.get('price'))
                # Заявка встречного токена приводится к нашей цене.
                ours = price if candidate == str(token) else round(1 - price, 6)
                row['order_size'] = float(order.get('original_size') or 0)
                break
            if ours is not None:
                break

        if ours is None:
            row.update({'verdict': 'без заявки',
                        'why': 'позиция есть, выхода из неё не выставлено'})
            rows.append(row)
            continue

        row['our_price'] = ours
        row['from_mid'] = round(abs(ours - top['mid']), 4)

        # ЦЕНА ЗА ПРЕДЕЛАМИ РЫНКА — НЕ ЗАЯВКА, А НАДЕЖДА. Продажа выше лучшего
        # аска исполнится только если рынок сам дойдёт до неё. Замерено:
        # держим по 0.637, продаём по 0.619, рынок 0.474 — четырнадцать центов
        # мимо, и порог «не продавать ниже себестоимости» не даёт подвинуться.
        beyond = (ours > top['ask'] + 1e-9 if side == 'ask'
                  else ours < top['bid'] - 1e-9)
        if beyond:
            row.update({
                'verdict': 'вне рынка',
                'why': ('продаём дороже, чем берут' if side == 'ask'
                        else 'покупаем дешевле, чем продают'),
                'queue_ahead': None})
            rows.append(row)
            continue

        ahead = _queue_ahead(live, side, ours, row.get('order_size'))
        row['queue_ahead'] = round(ahead, 1)
        if ahead <= 0:
            row.update({'verdict': 'первые в очереди',
                        'why': 'исполнят следующей встречной сделкой'})
        elif ahead > float(row.get('order_size') or 5) * 20:
            row.update({'verdict': 'за очередью',
                        'why': f'впереди {ahead:.0f} контрактов'})
        else:
            row.update({'verdict': 'ждём очереди',
                        'why': f'впереди {ahead:.0f} контрактов'})
        rows.append(row)

    rows.sort(key=lambda r: VERDICTS.index(r['verdict'])
              if r['verdict'] in VERDICTS else len(VERDICTS))
    return rows


def summary(rows):
    """Свод для панели: сколько под контролем и сколько требует внимания."""
    counts = {}
    for row in rows:
        counts[row['verdict']] = counts.get(row['verdict'], 0) + 1
    # ПОД КОНТРОЛЕМ — ЭТО «ЗАЯВКА СТОИТ ТАМ, ГДЕ ЕЁ МОГУТ ВЗЯТЬ». Всё
    # остальное требует либо времени, либо вмешательства, и складывать их в
    # одну кучу значило бы прятать второе за первым.
    working = sum(counts.get(name, 0) for name in
                  ('первые в очереди', 'ждём очереди'))
    return {
        'positions': len(rows),
        'working': working,
        'stuck': len(rows) - working,
        'by_verdict': counts,
        'value': round(sum(row.get('value') or 0 for row in rows), 2),
        'unrealised': round(sum(row.get('unrealised') or 0 for row in rows), 4),
        # Ближайший срок, когда бот сам что-то сделает. Без него «застряла»
        # читается как «навсегда», а это неправда.
        'next_action_hours': min(
            [row['until_market_exit'] for row in rows
             if row.get('until_market_exit') is not None] or [None],
            default=None),
    }
